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
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from backend.llm_adapter import (
    BaseCallbackHandler,
    BaseChatModel,
    LLMResult,
    build_chat_model,
)
from backend.config import settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ZZ.B1 #304-1 checkbox 3 (2026-04-24): LangChain message → turn.complete
# dict. Runs inside the adapter firewall's boundary (``llm.py`` is
# permitted to import from ``langchain`` indirectly via the wire up in
# llm_adapter.py). Kept as a module-level function so tests can pass
# synthetic dict-shaped objects with ``.type`` / ``.content`` attrs
# without instantiating a real BaseMessage.
_CHAT_ROLE_MAP = {
    "system": "system",
    "human": "user",
    "user": "user",
    "ai": "assistant",
    "assistant": "assistant",
    "tool": "tool",
    "function": "tool",
}


def _serialize_message(msg) -> dict:  # noqa: ANN001
    """Convert a LangChain message (or duck-typed shim) to the
    ``{role, content, tool_name?}`` shape the ``turn.complete`` event
    carries. Unknown message types degrade to ``role="user"`` with the
    repr so the payload still lands — the UI shows the raw line rather
    than silently dropping it.
    """
    raw_type = getattr(msg, "type", None) or getattr(msg, "role", None) or ""
    role = _CHAT_ROLE_MAP.get(str(raw_type).lower(), "user")
    content = getattr(msg, "content", "")
    # LangChain occasionally hands back structured content (list of
    # dict blocks). Stringify for the SSE payload — the drawer only
    # displays text today.
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content") or ""
                if text:
                    parts.append(str(text))
            else:
                parts.append(str(block))
        content = "\n".join(parts)
    elif not isinstance(content, str):
        content = str(content)
    tool_name = getattr(msg, "name", None)
    out: dict = {"role": role, "content": content}
    if tool_name:
        out["tool_name"] = tool_name
    return out


class TokenTrackingCallback(BaseCallbackHandler):
    """LangChain callback that feeds token usage into the system tracker.

    ZZ.A1 (#303-1): the callback also normalises prompt-cache hit / write
    counters across providers so downstream dashboards can surface a
    single ``cache_read`` / ``cache_create`` pair regardless of vendor
    shape. Anthropic reports both sides
    (``usage.cache_read_input_tokens`` + ``usage.cache_creation_input_tokens``);
    OpenAI only reports reads (``usage.prompt_tokens_details.cached_tokens``)
    and has no equivalent of cache creation — we normalise creation to 0
    for OpenAI rather than leaving it ``None`` so callers don't branch.
    The normalised pair is stashed on the instance
    (``last_cache_read`` / ``last_cache_create``) and plumbed through
    ``track_tokens`` → ``SharedTokenUsage.track`` so the lifetime
    cache counters + hit ratio land in both the in-memory dict and
    the ``token_usage`` Postgres row.
    """

    def __init__(self, model_name: str, provider: str | None = None) -> None:
        self.model_name = model_name
        # ZZ.A2 #303-2: ``provider`` threads the resolved provider id into
        # ``on_llm_end`` so the turn_metrics SSE event can look up the
        # context-window limit via ``get_context_limit(provider, model)``.
        # Kept as an optional kwarg for backward compatibility with test
        # fixtures that instantiate the callback directly without a
        # provider — lookups then return ``None`` and the UI degrades to
        # "—" per the NULL-vs-genuine-zero contract.
        self.provider = provider
        self._start: float = 0
        # ZZ.A3 (#303-3, 2026-04-24): ISO-8601 UTC wall-clock of the
        # most recent on_llm_start — stashed on the instance so
        # on_llm_end can hand both boundaries to track_tokens (and
        # through to SharedTokenUsage) in the same call. The
        # difference ``turn_ended_at - turn_started_at`` is pure LLM
        # compute; the gap between consecutive turns' stamps is the
        # tool-execution + event-bus-scheduling + context-gather
        # wait the ZZ.A3 dashboard surfaces.
        self._start_ts_utc: str = ""
        self.last_cache_read: int = 0
        self.last_cache_create: int = 0
        # ZZ.B1 #304-1 checkbox 3 (2026-04-24): prompt messages captured
        # at on_chat_model_start so the ``turn.complete`` emit in
        # on_llm_end can surface the full system / user / tool chain
        # to the TurnDetailDrawer. Stored as already-serialised dicts
        # so emit_turn_complete doesn't have to know about LangChain
        # message classes (the adapter firewall).
        self._prompt_messages: list[dict] = []

    def on_chat_model_start(  # noqa: ANN001
        self,
        serialized,
        messages,
        *args,
        **kwargs,
    ) -> None:
        """Capture prompt messages for the ``turn.complete`` payload.

        ZZ.B1 #304-1 checkbox 3: chat models give us the full prompt
        here (``messages: list[list[BaseMessage]]``). We flatten the
        first batch into ``{role, content, tool_name?}`` dicts and
        stash them on the instance; ``on_llm_end`` reads the stash and
        appends the assistant response before emitting.
        """
        self._start = time.time()
        self._start_ts_utc = datetime.now(timezone.utc).isoformat()
        try:
            flat = messages[0] if messages else []
        except (IndexError, TypeError):
            flat = []
        self._prompt_messages = [_serialize_message(m) for m in flat]

    def on_llm_start(self, *args, **kwargs) -> None:  # noqa: ANN002
        self._start = time.time()
        self._start_ts_utc = datetime.now(timezone.utc).isoformat()
        # Non-chat / completion models don't invoke on_chat_model_start;
        # clear the stash so a stale prompt from a prior chat turn
        # does not leak into a subsequent non-chat ``turn.complete``.
        self._prompt_messages = []

    @staticmethod
    def _extract_cache_tokens(usage: dict) -> tuple[int, int]:
        """Return (cache_read, cache_create) normalised across providers.

        Priority order:
        1. Anthropic-native keys on ``usage`` itself
           (``cache_read_input_tokens`` / ``cache_creation_input_tokens``).
        2. OpenAI-native nested dict
           (``prompt_tokens_details.cached_tokens``; no creation side).
        3. LangChain's unified ``usage_metadata.input_token_details``
           shape (``cache_read`` / ``cache_creation``), which some
           langchain-anthropic / langchain-openai versions surface when
           ``llm_output`` is empty.
        Missing fields default to 0 — they're additive counters where
        "absent" and "zero" are indistinguishable to the dashboard.
        """
        if not isinstance(usage, dict):
            return 0, 0

        cache_read = usage.get("cache_read_input_tokens")
        cache_create = usage.get("cache_creation_input_tokens")
        if cache_read is not None or cache_create is not None:
            return int(cache_read or 0), int(cache_create or 0)

        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and "cached_tokens" in details:
            return int(details.get("cached_tokens") or 0), 0

        itd = usage.get("input_token_details")
        if isinstance(itd, dict) and ("cache_read" in itd or "cache_creation" in itd):
            return int(itd.get("cache_read") or 0), int(itd.get("cache_creation") or 0)

        return 0, 0

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:  # noqa: ANN003
        try:
            from backend.routers.system import track_tokens

            latency_ms = int((time.time() - self._start) * 1000)
            usage: dict = {}
            if response.llm_output:
                usage = response.llm_output.get("token_usage", {})
                if not usage:
                    usage = response.llm_output.get("usage", {})
            # Some providers (notably langchain-anthropic ≥ 0.3) expose
            # usage only on per-generation ``usage_metadata`` when
            # ``llm_output`` is empty — fall back to the first generation
            # so cache counters aren't silently lost.
            if not usage:
                try:
                    gen = response.generations[0][0]
                    msg = getattr(gen, "message", None)
                    meta = getattr(msg, "usage_metadata", None) if msg else None
                    if isinstance(meta, dict):
                        usage = meta
                except (AttributeError, IndexError, TypeError):
                    usage = {}

            cache_read, cache_create = self._extract_cache_tokens(usage)
            self.last_cache_read = cache_read
            self.last_cache_create = cache_create

            # ZZ.A1 (#303-1, 2026-04-24): propagate normalised cache
            # counters into ``track_tokens`` so ``SharedTokenUsage``
            # and the ``token_usage`` row both accumulate the lifetime
            # totals + recomputed hit ratio.
            # ZZ.A3 (#303-3, 2026-04-24): also plumb the
            # on_llm_start / on_llm_end wall-clock stamps through so
            # the dashboard can derive per-turn LLM compute time +
            # inter-turn gap. ``turn_ended_at`` is captured here
            # (as close to the track_tokens call as possible) so the
            # stored value reflects "LLM call completed" rather than
            # "track_tokens invoked", which may drift slightly if the
            # cache-extract codepath above spent cycles.
            input_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
            turn_ended_at = datetime.now(timezone.utc).isoformat()
            track_tokens(
                self.model_name,
                input_tokens,
                output_tokens,
                latency_ms,
                cache_read_tokens=cache_read,
                cache_create_tokens=cache_create,
                turn_started_at=self._start_ts_utc or None,
                turn_ended_at=turn_ended_at,
            )

            # ZZ.A2 #303-2: emit per-turn context-usage snapshot so the
            # TokenUsageStats card can render a live progress bar + warning
            # icon against the provider's advertised context window. The
            # limit lookup honours the NULL-vs-genuine-zero contract —
            # ``None`` (unknown provider/model / Ollama without override /
            # OpenRouter pass-through) propagates through as
            # ``context_usage_pct=None`` so the UI renders "—" rather than
            # a fabricated zero. Best-effort: an emit failure must not
            # abort the LLM turn.
            context_limit: int | None = None
            try:
                from backend.context_limits import get_context_limit
                from backend.events import emit_turn_metrics

                context_limit = get_context_limit(self.provider, self.model_name)
                emit_turn_metrics(
                    self.model_name,
                    int(input_tokens or 0),
                    int(output_tokens or 0),
                    latency_ms,
                    provider=self.provider,
                    context_limit=context_limit,
                    cache_read_tokens=cache_read,
                    cache_create_tokens=cache_create,
                    broadcast_scope="global",
                )
            except Exception as exc:
                logger.debug("turn_metrics emit skipped: %s", exc)

            # ZZ.B1 #304-1 checkbox 3 (2026-04-24): terminal ``turn.complete``
            # event with the rich payload the TurnDetailDrawer needs
            # (prompt + assistant messages, backend-authoritative cost).
            # Fires *after* emit_turn_metrics so the frontend's ring
            # buffer has already materialised the bare turn by the time
            # the drawer-worthy details arrive; turn.complete then
            # upgrades the existing card in place.
            try:
                import uuid as _uuid
                from backend.events import emit_turn_complete

                # Extract the assistant response from the first
                # generation. ``AIMessage`` serialises to
                # ``{role:"assistant", content:...}``; if the provider
                # handed back plain text we synthesise the same shape.
                assistant_msg: dict | None = None
                summary_text: str | None = None
                try:
                    gen = response.generations[0][0]
                    a_msg = getattr(gen, "message", None)
                    if a_msg is not None:
                        assistant_msg = _serialize_message(a_msg)
                    elif getattr(gen, "text", None):
                        assistant_msg = {"role": "assistant", "content": gen.text}
                except (AttributeError, IndexError, TypeError):
                    assistant_msg = None

                all_messages = list(self._prompt_messages)
                if assistant_msg is not None:
                    all_messages.append(assistant_msg)
                    if isinstance(assistant_msg.get("content"), str):
                        summary_text = assistant_msg["content"][:200]

                emit_turn_complete(
                    turn_id=f"turn-{_uuid.uuid4().hex[:12]}",
                    model=self.model_name,
                    input_tokens=int(input_tokens or 0),
                    output_tokens=int(output_tokens or 0),
                    latency_ms=latency_ms,
                    provider=self.provider,
                    context_limit=context_limit,
                    cache_read_tokens=cache_read,
                    cache_create_tokens=cache_create,
                    messages=all_messages,
                    tool_calls=[],
                    started_at=self._start_ts_utc or None,
                    ended_at=turn_ended_at,
                    summary=summary_text,
                    broadcast_scope="global",
                )
            except Exception as exc:
                logger.debug("turn.complete emit skipped: %s", exc)
        except Exception as exc:
            logger.warning("Token tracking failed for %s: %s", self.model_name, exc)

# Cache to avoid re-creating LLM instances
_cache: dict[str, BaseChatModel] = {}
_provider_failures: dict[str, float] = {}  # provider → last_failure_timestamp
PROVIDER_COOLDOWN = 300  # 5 minutes — don't retry a failed provider within this window
_PROVIDER_FAILURES_MAX = 256  # cap to bound memory

# Lock guards composite read-modify-write on _provider_failures (record +
# prune). CPython single dict ops are atomic, but iteration during prune
# from another thread/coroutine would raise RuntimeError.
import threading as _threading
_provider_failures_lock = _threading.Lock()


def _record_provider_failure(provider: str, ts: float | None = None,
                              *, reason: str | None = None) -> None:
    """Record a provider failure timestamp; prune stale entries to bound size.

    Also records the failure on the per-tenant-per-key circuit breaker
    (M3) so a single tenant's bad key cannot affect other tenants.

    The legacy global ``_provider_failures`` dict is kept in sync for
    backward compatibility (existing callers / metrics / tests still
    read it), but the *authoritative* state for failover decisions is
    now ``backend.circuit_breaker``.
    """
    import time as _t
    now = _t.time()
    with _provider_failures_lock:
        _provider_failures[provider] = ts if ts is not None else now
        if len(_provider_failures) > _PROVIDER_FAILURES_MAX:
            cutoff = now - 86400
            for k in [k for k, v in _provider_failures.items() if v < cutoff]:
                _provider_failures.pop(k, None)
            while len(_provider_failures) > _PROVIDER_FAILURES_MAX:
                oldest = min(_provider_failures, key=_provider_failures.get)
                _provider_failures.pop(oldest, None)
    try:
        from backend import circuit_breaker
        from backend.db_context import current_tenant_id
        tid = current_tenant_id() or "t-default"
        fp = circuit_breaker.active_fingerprint(provider)
        circuit_breaker.record_failure(tid, provider, fp, reason=reason)
    except Exception as exc:
        logger.debug("circuit_breaker.record_failure skipped: %s", exc)


def _record_provider_success(provider: str) -> None:
    """Mark the per-tenant-per-key circuit as closed after a healthy call."""
    try:
        from backend import circuit_breaker
        from backend.db_context import current_tenant_id
        tid = current_tenant_id() or "t-default"
        fp = circuit_breaker.active_fingerprint(provider)
        circuit_breaker.record_success(tid, provider, fp)
    except Exception as exc:
        logger.debug("circuit_breaker.record_success skipped: %s", exc)


def _per_tenant_circuit_open(provider: str) -> bool:
    """Return True if the per-tenant per-key circuit is open for the
    *current* request context.  Falls back to False on any error so the
    breaker never blocks the happy path due to its own bug.
    """
    try:
        from backend import circuit_breaker
        from backend.db_context import current_tenant_id
        tid = current_tenant_id() or "t-default"
        fp = circuit_breaker.active_fingerprint(provider)
        return circuit_breaker.is_open(tid, provider, fp)
    except Exception as exc:
        logger.debug("circuit_breaker.is_open skipped: %s", exc)
        return False


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
    if _sys_mod.is_token_frozen():
        logger.info("Token budget frozen — LLM disabled, using rule-based fallback")
        return None

    provider = provider or settings.llm_provider
    # Per-provider model resolution:
    #   1. Explicit caller override wins.
    #   2. Primary provider → ``settings.get_model_name()`` which honours
    #      ``settings.llm_model`` (so Anthropic can be pinned to
    #      ``claude-opus-4-7`` etc.).
    #   3. Ollama as fallback → ``settings.ollama_model`` (when set);
    #      otherwise let ``build_chat_model`` use its hardcoded
    #      ``llama3.1`` default. This is the Phase-2 wire-up escape
    #      hatch — ``llm_model`` is Anthropic-shaped and cannot be
    #      reused for ollama without mis-routing.
    #   4. Any other non-primary provider → ``None`` and the adapter
    #      falls back to its own hardcoded default.
    if model is None:
        if provider == settings.llm_provider:
            model = settings.get_model_name()
        elif provider == "ollama":
            ollama_default = (getattr(settings, "ollama_model", "") or "").strip()
            if ollama_default:
                model = ollama_default

    cache_key = f"{provider}:{model}:{id(bind_tools) if bind_tools else 'none'}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        llm = _create_llm(provider, model)

        # Failover: if primary fails, try fallback chain with cooldown.
        # M3: cooldown decisions consult the per-tenant per-key breaker
        # so one tenant's bad key cannot push other tenants down-chain.
        if llm is None:
            # Primary provider also failed — record so its breaker opens.
            _record_provider_failure(provider, reason="primary_init_failed")
            chain = [p.strip() for p in settings.llm_fallback_chain.split(",") if p.strip()]
            for fallback_provider in chain:
                if fallback_provider == provider:
                    continue  # Skip the one that already failed
                # Per-tenant per-key breaker takes precedence; legacy
                # global cooldown is consulted as a secondary guard so
                # operator-set bypasses still work (and tests that
                # manipulate _provider_failures directly keep passing).
                if _per_tenant_circuit_open(fallback_provider):
                    logger.debug("Skipping %s (per-tenant circuit open)", fallback_provider)
                    continue
                last_fail = _provider_failures.get(fallback_provider, 0)
                if time.time() - last_fail < PROVIDER_COOLDOWN:
                    logger.debug("Skipping %s (legacy cooldown, failed %ds ago)", fallback_provider, int(time.time() - last_fail))
                    continue
                try:
                    llm = _create_llm(fallback_provider, None)
                except Exception as exc:
                    _record_provider_failure(fallback_provider, reason=str(exc)[:120])
                    continue
                if llm is not None:
                    provider = fallback_provider
                    model = None
                    _record_provider_success(fallback_provider)
                    logger.info("Failover: %s → %s", settings.llm_provider, fallback_provider)
                    break
                else:
                    _record_provider_failure(fallback_provider, reason="missing_credentials")
            if llm is None:
                from backend.events import emit_token_warning
                emit_token_warning("all_providers_failed", "All LLM providers failed. Using rule-based fallback.")
                return None
        else:
            # Primary succeeded; close any prior circuit for this key.
            _record_provider_success(provider)

        # Inject token tracking callback (graceful if provider doesn't support it)
        model_name = model or (llm.model_name if hasattr(llm, "model_name") else f"{provider}:default")
        try:
            llm = llm.with_config(callbacks=[TokenTrackingCallback(model_name, provider=provider)])
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
    """Instantiate a configured chat model via the adapter firewall.

    All provider-specific instantiation logic (class imports, argument
    shapes, base URLs) lives in `backend.llm_adapter.build_chat_model`.
    This function is now just a settings-lookup + credential-gate shim.
    """
    temp = settings.llm_temperature

    # Provider → (api_key_attr, extra kwargs) — the adapter knows the
    # default model name, base_url, and class to use for each provider.
    _PROVIDER_CREDS: dict[str, tuple[str | None, dict]] = {
        "anthropic": ("anthropic_api_key", {}),
        "google": ("google_api_key", {}),
        "openai": ("openai_api_key", {}),
        "xai": ("xai_api_key", {}),
        "groq": ("groq_api_key", {}),
        "deepseek": ("deepseek_api_key", {}),
        "together": ("together_api_key", {}),
        "openrouter": ("openrouter_api_key", {
            "default_headers": {
                "HTTP-Referer": "https://omnisight.local",
                "X-Title": "OmniSight Productizer",
            },
        }),
        # Ollama is keyless but needs base_url threaded through
        "ollama": (None, {"base_url": settings.ollama_base_url}),
    }

    if provider not in _PROVIDER_CREDS:
        logger.warning("Unknown LLM provider: %s", provider)
        return None

    key_attr, extra_kwargs = _PROVIDER_CREDS[provider]
    api_key: str | None = None
    if key_attr is not None:
        api_key = getattr(settings, key_attr, None)
        if not api_key:
            logger.info("No OMNISIGHT_%s_API_KEY set", provider.upper())
            return None

    try:
        return build_chat_model(
            provider=provider,
            model=model,
            temperature=temp,
            api_key=api_key,
            **extra_kwargs,
        )
    except (ValueError, ImportError) as exc:
        logger.warning("Failed to build chat model for %s: %s", provider, exc)
        return None


def list_providers() -> list[dict]:
    """Return metadata about all supported providers."""
    providers = [
        {
            "id": "anthropic",
            "name": "Anthropic",
            "default_model": "claude-sonnet-4-20250514",
            "models": [
                "claude-opus-4-7",
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
        return {"valid": True, "provider": settings.llm_provider, "model": model, "configured": True,
                "warning": f"Model '{model}' not found in any provider — will use global default"}

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

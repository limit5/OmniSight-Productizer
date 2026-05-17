"""Multi-provider LLM factory + observability callback.

Single entry point for every LangChain chat model the backend creates.
Concentrates provider knowledge (credential resolution, model defaults,
adapter kwargs, fallback chain) in one module so the rest of the
codebase imports a generic ``BaseChatModel`` and stays vendor-agnostic.

Supported providers
-------------------
Anthropic (default), Google Gemini, OpenAI, xAI (Grok), Groq, DeepSeek,
Together.ai, OpenRouter, and Ollama (keyless local runtime). Provider
metadata + the public model whitelist are surfaced through
:func:`list_providers` and consumed by the Settings UI.

Public surface
--------------
- :func:`get_llm` — primary factory; honours per-cache, per-tenant
  circuit breakers (M3), and the configured fallback chain.
- :func:`get_cheapest_model` — cost-aware factory for utility LLM
  calls (auto-title, future short-form classifiers) — walks
  :data:`_CHEAPEST_MODEL_PREFERENCE` and intentionally disables the
  cascade so a missing cheap-key does not silently land on flagship Opus.
- :func:`list_providers` — provider registry consumed by the Settings UI.
- :func:`validate_model_spec` — pre-flight check used by routes that
  accept user-supplied model strings.
- :class:`TokenTrackingCallback` — LangChain callback that feeds usage
  + rate-limit + cache + per-turn-metric telemetry into the shared
  pipelines (``SharedTokenUsage``, ``emit_turn_metrics``,
  ``emit_turn_complete``, ``SharedKV("provider_ratelimit")``).

Side-effect pipelines (per LLM turn)
------------------------------------
``TokenTrackingCallback.on_llm_end`` fans out into four downstream
systems, each wrapped in its own try/except so a single subsystem
failure cannot abort the user-visible turn:

1. ``track_tokens`` → ``SharedTokenUsage`` + Postgres ``token_usage`` row
   (ZZ.A1 cache normalisation, ZZ.A3 wall-clock stamps).
2. ``emit_turn_metrics`` SSE — live ring-buffer card in the dashboard
   (ZZ.A2 context-window progress + warning icon).
3. ``emit_turn_complete`` SSE — rich payload for ``TurnDetailDrawer``
   (ZZ.B1 prompt + assistant messages, backend-authoritative cost).
4. ``SharedKV("provider_ratelimit")`` write with 60 s TTL (Z.1 #290 ck-3)
   so cross-worker dashboards + future adaptive backoff (Z.2 / Z.4)
   read the latest rate-limit snapshot without holding a callback ref.

Failover + resilience
---------------------
Failed primary inits walk ``settings.llm_fallback_chain`` in order. Two
breakers gate each candidate:

- **Per-tenant per-key circuit** (``backend.circuit_breaker``) — opens
  on repeated failure for a single ``(tenant_id, provider, key_fp)``
  so one tenant's bad key cannot push others down-chain.
- **Legacy global cooldown** (``_provider_failures``, 5 min) — kept in
  sync for backward compatibility with older callers / metrics / tests
  that read it directly.

Caching
-------
Process-local cache keyed by ``f"{provider}:{model}:{id(bind_tools)}"``
so tool bindings are not silently shared between callers that bind
different tool lists. The cache is per-worker; cross-worker coordination
is intentionally left to the rate-limit / circuit-breaker subsystems.

Usage
-----
    from backend.agents.llm import get_llm, get_cheapest_model
    llm = get_llm()                    # configured default provider
    llm = get_llm("openai")            # override provider
    llm = get_llm("groq", "mixtral-8x7b-32768")  # override provider + model
    llm = get_cheapest_model()         # utility-call tier (no Opus burn)
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from backend.llm_adapter import (
    BaseCallbackHandler,
    BaseChatModel,
    LLMResult,
    build_chat_model,
)
from backend.config import settings
# OP-80: import canonical key names + provider-header dict from the
# shared contract module. ratelimit_headers.py (legacy module) still
# re-exports _PROVIDER_RATELIMIT_HEADERS for any older Z.1 callers; the
# refactor in this commit makes the contract module the source of truth.
from backend.agents.ratelimit_contract import (
    PROVIDER_RATELIMIT_HEADER_KEYS,
    REMAINING_REQUESTS_KEY,
    REMAINING_TOKENS_KEY,
    RESET_AT_TS_KEY,
    RETRY_AFTER_S_KEY,
)
from backend.agents.ratelimit_headers import _PROVIDER_RATELIMIT_HEADERS

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_OLLAMA_TOOL_COMPAT_PATH = (
    Path(__file__).parents[2] / "config" / "ollama_tool_calling.yaml"
)


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


# ─────────────────────────────────────────────────────────────────
# Z.1 (#290) checkbox 2 (2026-04-24): per-provider rate-limit header
# name → unified-key mapping.
# ─────────────────────────────────────────────────────────────────
#
# The eight adapter providers cluster into two schema families:
#
#   - **Anthropic native** — emits
#     ``anthropic-ratelimit-{requests,tokens}-{remaining,reset}`` where
#     the ``-reset`` values are RFC 3339 absolute timestamps.
#   - **OpenAI-compatible** — the ``/v1/chat/completions`` wire
#     contract used by OpenAI, xAI, Groq, DeepSeek, Together, and
#     OpenRouter. Emits ``x-ratelimit-{remaining,reset}-{requests,tokens}``
#     where ``-reset`` values are *duration* strings (``"12ms"``,
#     ``"1s"``, ``"1m30s"``) measured from response time.
#
# Ollama is deliberately absent: the local runtime has no HTTP rate
# limits, so ``_normalize_ratelimit_headers`` returns ``{}`` for it
# and the downstream SharedKV write (later Z.1 checkbox) skips the
# provider on truthiness. A provider added later without a row here
# gets the same "skip" treatment, which is safer than guessing a
# schema — the operator's 429 logs will surface the new provider and
# the mapping can be added explicitly.
#
# Picking token-reset over request-reset for the single ``reset_at`` slot:
# LLM traffic is token-dominated; the request bucket almost never binds
# before the token bucket. If the unified dict later needs to expose
# both, split into ``reset_requests_at_ts`` / ``reset_tokens_at_ts`` —
# but right now one field keeps the SharedKV payload + UI card simple.
#
# Module-global audit (SOP Step 1): _PROVIDER_RATELIMIT_HEADERS is now
# imported (above) from backend.agents.ratelimit_headers, which itself
# re-exports the canonical PROVIDER_RATELIMIT_HEADER_KEYS dict from
# backend.agents.ratelimit_contract. Single source of truth, no
# duplicate dict literal needed here.
#
# Google Gemini uses a gRPC/REST API; LangChain's langchain-google-genai
# does not currently surface per-request rate-limit headers through any
# of the 5 paths ``_extract_response_headers`` walks, so it's omitted
# from the contract alongside Ollama. Revisit if an adapter version
# lands that mirrors the SDK's ``x-goog-quota-*`` headers.


_DURATION_RE = re.compile(
    r"^\s*"
    r"(?:(?P<h>\d+)h)?"
    r"(?:(?P<m>\d+)m(?!s))?"
    r"(?:(?P<s>\d+(?:\.\d+)?)s)?"
    r"(?:(?P<ms>\d+(?:\.\d+)?)ms)?"
    r"\s*$"
)


def _parse_duration_seconds(val) -> float | None:  # noqa: ANN001
    """Parse an OpenAI-style duration string (``"12ms"`` / ``"1s"`` /
    ``"1m30s"`` / ``"2h"``) into total seconds. Returns ``None`` for
    anything the grammar doesn't recognise — the caller decides whether
    to fall through to a bare-float parse or give up."""
    if not isinstance(val, str):
        return None
    s = val.strip()
    if not s:
        return None
    m = _DURATION_RE.match(s)
    if not m or not any(v is not None for v in m.groupdict().values()):
        return None
    total = 0.0
    if m.group("h"):
        total += int(m.group("h")) * 3600
    if m.group("m"):
        total += int(m.group("m")) * 60
    if m.group("s"):
        total += float(m.group("s"))
    if m.group("ms"):
        total += float(m.group("ms")) / 1000.0
    return total


_ISO8601_HINT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T")


def _parse_reset_value(val, *, now_fn=None) -> float | None:  # noqa: ANN001
    """Parse a provider-shaped reset-timestamp value into a unix epoch
    (seconds, float).

    Handles three shapes observed across the seven supported providers:

    1. **RFC 3339 absolute** — Anthropic (``"2026-04-24T13:00:00Z"``,
       also any ``±HH:MM`` offset). Returned as-is after epoch
       conversion.
    2. **Duration offset** — OpenAI-compatible family (``"12ms"``,
       ``"1s"``, ``"1m30s"``). Added to current wall-clock so every
       downstream reader agrees on an absolute frame of reference;
       this loses a few ms of precision versus anchoring at HTTP
       response-received time, which is fine for a dashboard card.
    3. **Bare numeric seconds** — some gateways strip the unit suffix
       and emit ``"30"``. Treated as a seconds offset from now.

    Malformed / empty input → ``None`` (never raises).

    ``now_fn`` is injectable so tests can freeze the clock without
    monkey-patching ``time`` globally — ``None`` (default) resolves
    lazily to ``time.time`` so a monkeypatch on ``time.time`` at the
    call site still takes effect.
    """
    if not val or not isinstance(val, str):
        return None
    s = val.strip()
    if _ISO8601_HINT_RE.match(s):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, TypeError):
            pass
    _now = now_fn if now_fn is not None else time.time
    offset = _parse_duration_seconds(s)
    if offset is not None:
        return _now() + offset
    try:
        return _now() + float(s)
    except (ValueError, TypeError):
        return None


def _parse_retry_after_seconds(val, *, now_fn=None) -> float | None:  # noqa: ANN001
    """Parse an HTTP ``Retry-After`` value into seconds-from-now.

    Per RFC 9110 §10.2.3, this header is either a non-negative integer
    of seconds or an HTTP-date. Modern providers always emit the former,
    but we decode the latter defensively so a CDN-inserted rewrite
    (Cloudflare sometimes swaps in a date form) doesn't silently give
    us ``None``.

    ``now_fn=None`` resolves lazily to ``time.time`` so a test-time
    monkeypatch on ``time.time`` still takes effect for the HTTP-date
    branch.
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        return max(0.0, float(s))
    except (ValueError, TypeError):
        pass
    _now = now_fn if now_fn is not None else time.time
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return max(0.0, dt.timestamp() - _now())
    except (ValueError, TypeError, IndexError):
        pass
    return None


def _parse_int_or_none(val) -> int | None:  # noqa: ANN001
    """Coerce a header value to int. Float strings are accepted (some
    providers emit ``"42.0"``); negatives are preserved rather than
    clamped to 0 since a negative remaining would itself signal an
    unexpected upstream state worth surfacing to the dashboard."""
    if val is None:
        return None
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return None


def _normalize_ratelimit_headers(
    provider: str | None,
    headers: dict | None,
) -> dict:
    """Regularise provider-specific rate-limit headers into a unified
    ``{remaining_requests, remaining_tokens, reset_at_ts, retry_after_s}``
    dict.

    Z.1 (#290) checkbox 2. Rules:

    - Unknown / unmapped provider (Ollama, Google Gemini today, any
      future adapter lacking a row) → ``{}``. Caller branches on
      truthiness and skips the SharedKV write (the next checkbox).
    - Empty or non-dict ``headers`` → ``{}`` (same truthiness contract).
    - For mapped providers, header lookup is case-insensitive — SDKs
      normalise to lowercase but an intermediate ``httpx.Headers``
      view can preserve mixed case, and defending here costs nothing.
    - Missing individual fields degrade to ``None`` (not ``0``) —
      preserves the NULL-vs-genuine-zero contract ZZ.A1 established
      so the dashboard can draw "—" for "unknown" separately from
      "0 remaining".
    - If every field is ``None``, the result collapses back to ``{}``
      so "known provider, but this response carried no rate-limit
      headers at all" is indistinguishable from "unknown provider"
      for downstream purposes.

    Never raises: a malformed individual field is swallowed by the
    per-field parse helpers and emerges as ``None``.
    """
    if not provider or not isinstance(headers, dict) or not headers:
        return {}
    mapping = _PROVIDER_RATELIMIT_HEADERS.get(provider)
    if mapping is None:
        return {}
    lower = {
        k.lower(): v for k, v in headers.items() if isinstance(k, str)
    }
    raw_req = lower.get(mapping[REMAINING_REQUESTS_KEY].lower())
    raw_tok = lower.get(mapping[REMAINING_TOKENS_KEY].lower())
    raw_reset = lower.get(mapping["reset_at"].lower())
    raw_retry = lower.get(mapping["retry_after"].lower())

    result: dict = {
        REMAINING_REQUESTS_KEY: _parse_int_or_none(raw_req),
        REMAINING_TOKENS_KEY: _parse_int_or_none(raw_tok),
        RESET_AT_TS_KEY: _parse_reset_value(raw_reset),
        RETRY_AFTER_S_KEY: _parse_retry_after_seconds(raw_retry),
    }
    if all(v is None for v in result.values()):
        return {}
    return result


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
        """Initialise the per-turn telemetry buffers.

        Args:
            model_name: Concrete model id used for the LangChain call
                (e.g. ``"claude-opus-4-7"``). Threaded into every
                downstream emit (``track_tokens``, ``emit_turn_metrics``,
                ``emit_turn_complete``) as the authoritative model label.
            provider: Resolved provider id (``"anthropic"``, ``"openai"``
                …). Optional for backward-compat with test fixtures that
                instantiate the callback directly; when ``None``, the
                context-window lookup degrades to "—" and the rate-limit
                SharedKV write is skipped.

        All ``last_*`` attributes are per-turn buffers populated in
        ``on_llm_end`` and read by tests / observability code that
        needs to inspect what the most recent turn carried — see the
        inline comments below for the contract each one upholds.
        """
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
        # Z.1 (#290) checkbox 1 (2026-04-24): raw HTTP response headers
        # snapshotted from the underlying SDK on each on_llm_end. The
        # dict is kept unnormalised here — later Z.1 checkboxes own the
        # per-provider name mapping (``anthropic-ratelimit-*`` vs
        # ``x-ratelimit-remaining-requests`` vs …) and the SharedKV
        # write. Empty dict (not ``None``) when headers can't be
        # located so readers branch on truthiness rather than identity.
        self.last_response_headers: dict = {}
        # Z.1 (#290) checkbox 2 (2026-04-24): the post-``on_llm_end``
        # normalised rate-limit snapshot produced by
        # ``_normalize_ratelimit_headers`` — keys are the unified
        # ``{remaining_requests, remaining_tokens, reset_at_ts,
        # retry_after_s}`` set. Empty dict when either the provider is
        # unmapped (Ollama / Google Gemini today) or the response
        # carried no rate-limit headers. The subsequent Z.1 checkbox
        # reads this attribute and mirrors it into
        # ``SharedKV("provider_ratelimit")`` with a 60s TTL.
        self.last_ratelimit_state: dict = {}

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
        """Reset per-turn timing state for non-chat completion models.

        LangChain dispatches one of two start hooks: chat models hit
        :meth:`on_chat_model_start` (which captures the prompt batch
        for the ``turn.complete`` payload), text-completion models hit
        this one. Both must stamp ``self._start`` /
        ``self._start_ts_utc`` so :meth:`on_llm_end` can compute
        ``latency_ms`` and the ZZ.A3 ``turn_started_at`` boundary.
        """
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

    @staticmethod
    def _extract_response_headers(response: LLMResult) -> dict:
        """Fish the raw rate-limit / response header dict out of a
        LangChain ``LLMResult``.

        Z.1 (#290) checkbox 1 (2026-04-24). LangChain does not expose a
        single stable path for the underlying SDK's ``.response.headers``
        dict — different ``langchain-<provider>`` versions stash it in
        different places, and a LangChain minor-version bump routinely
        moves it. Rather than binding to one path and silently breaking
        on the next upgrade, we walk every location observed to date
        and return the first non-empty hit. Order is most-authoritative
        first:

        1. ``llm_output['headers']`` — langchain-openai ≥ 0.2 mirror of
           ``openai.AsyncOpenAI`` ``.response.headers``.
        2. ``llm_output['response_headers']`` — alternate name some
           adapters use.
        3. ``generations[0][0].message.response_metadata['headers']`` —
           langchain-anthropic ≥ 0.3 path (raw response metadata).
        4. ``generations[0][0].message.response_metadata`` flattened —
           some adapters inline the ``x-ratelimit-*`` /
           ``anthropic-ratelimit-*`` keys directly into
           ``response_metadata`` without a ``headers`` wrapper.
        5. ``generations[0][0].generation_info['headers']`` — older
           adapters kept raw headers here.

        Returns ``{}`` (not ``None``) when no headers can be located —
        matches the downstream expectation that "absent" and "empty"
        are indistinguishable (Ollama emits no rate-limit headers at
        all and is a legitimate empty-dict case). Never raises: a
        malformed provider response or a LangChain-upgrade drift
        must degrade to ``{}`` so the LLM turn completes cleanly.
        """
        if not isinstance(response, LLMResult):
            return {}

        llm_out = getattr(response, "llm_output", None)
        if isinstance(llm_out, dict):
            for key in ("headers", "response_headers"):
                cand = llm_out.get(key)
                if isinstance(cand, dict) and cand:
                    return dict(cand)

        try:
            gen = response.generations[0][0]
        except (AttributeError, IndexError, TypeError):
            return {}

        msg = getattr(gen, "message", None)
        meta = getattr(msg, "response_metadata", None) if msg else None
        if isinstance(meta, dict):
            wrapped = meta.get("headers")
            if isinstance(wrapped, dict) and wrapped:
                return dict(wrapped)
            # Flattened rate-limit metadata — detect by the characteristic
            # header-name prefixes any of the four supported providers
            # emit. Pattern matches both vendor-specific
            # ``anthropic-ratelimit-*`` + the generic ``x-ratelimit-*``
            # that OpenAI / xAI / Groq / DeepSeek all share, plus the
            # bare ``retry-after`` the 429 path sets.
            rl_prefixes = ("x-ratelimit", "anthropic-ratelimit")
            rl_exact = {"retry-after"}
            if any(
                isinstance(k, str)
                and (k.lower().startswith(rl_prefixes) or k.lower() in rl_exact)
                for k in meta.keys()
            ):
                return dict(meta)

        gen_info = getattr(gen, "generation_info", None)
        if isinstance(gen_info, dict):
            cand = gen_info.get("headers")
            if isinstance(cand, dict) and cand:
                return dict(cand)

        return {}

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:  # noqa: ANN003
        """Fan out the completed turn into every downstream telemetry pipeline.

        Invoked by LangChain after the provider's HTTP response has been
        materialised into an :class:`LLMResult`. Drives, in order:

        1. **Rate-limit snapshot + normalisation + SharedKV mirror**
           (Z.1 #290 ck-1/2/3). Each step has its own try/except so a
           parse bug on an unfamiliar provider shape never propagates
           into the user-visible turn.
        2. **Token usage extraction** (with langchain-anthropic ≥ 0.3
           ``usage_metadata`` fallback) and cache-counter normalisation
           via :meth:`_extract_cache_tokens` (ZZ.A1).
        3. **``track_tokens``** — feeds ``SharedTokenUsage`` + the
           Postgres ``token_usage`` row with the input/output/cache
           counts, latency, and ZZ.A3 wall-clock stamps.
        4. **``emit_turn_metrics``** — SSE event powering the live
           dashboard ring-buffer card with context-window % (ZZ.A2).
           ``context_limit=None`` is honoured per the NULL-vs-genuine-zero
           contract so unknown providers render "—" not "0%".
        5. **``emit_turn_complete``** — rich SSE payload that upgrades
           the bare turn card into a ``TurnDetailDrawer``-ready record
           with the full prompt + assistant message chain (ZZ.B1).

        Every subsystem is wrapped so a single failure (Redis drop,
        emit-bus hiccup, malformed response) degrades to a debug log
        rather than aborting the LLM call. The outermost ``except``
        downgrades to a single ``logger.warning`` so observability bugs
        cannot starve the application of LLM responses.
        """
        try:
            from backend.routers.system import track_tokens

            latency_ms = int((time.time() - self._start) * 1000)
            # Z.1 (#290) checkbox 1: snapshot the raw response headers
            # onto the callback instance before token-usage processing
            # so a later exception in the token/metrics pipeline does
            # not silently drop the rate-limit snapshot. Downstream Z.1
            # checkboxes (name mapping + SharedKV write) read from
            # ``self.last_response_headers``; empty dict is the
            # "no headers / unknown provider / Ollama" case and is
            # expected — debug log only, never raise.
            try:
                self.last_response_headers = self._extract_response_headers(response)
                if not self.last_response_headers:
                    logger.debug(
                        "No rate-limit headers on %s response (empty dict)",
                        self.model_name,
                    )
            except Exception as exc:
                logger.debug(
                    "rate-limit header extraction skipped for %s: %s",
                    self.model_name, exc,
                )
                self.last_response_headers = {}
            # Z.1 (#290) checkbox 2: normalise the raw headers into the
            # unified ``{remaining_requests, remaining_tokens,
            # reset_at_ts, retry_after_s}`` dict and stash it on the
            # instance. Own try/except so a normalise bug (e.g. the
            # provider emits a reset-timestamp shape we haven't seen)
            # degrades to ``{}`` rather than aborting the LLM turn — the
            # raw snapshot above is already safe. The next checkbox
            # reads ``self.last_ratelimit_state`` and mirrors it into
            # ``SharedKV("provider_ratelimit")``.
            try:
                self.last_ratelimit_state = _normalize_ratelimit_headers(
                    self.provider, self.last_response_headers,
                )
            except Exception as exc:
                logger.debug(
                    "rate-limit header normalisation skipped for %s: %s",
                    self.model_name, exc,
                )
                self.last_ratelimit_state = {}
            # Z.1 (#290) checkbox 3: mirror the normalised snapshot into
            # ``SharedKV("provider_ratelimit")`` with a 60 s per-field
            # TTL so dashboard polling endpoints + future adaptive-
            # backoff logic (Z.2 / Z.4) can read the latest state
            # cross-worker without holding a callback reference.
            #
            # Skip conditions — all silent, none raise:
            #   - ``self.provider`` is None: callback instantiated via
            #     legacy fixture that predates the ``provider`` kwarg.
            #     Not a hard-fail; the snapshot will land on the next
            #     live turn that carries a provider.
            #   - ``self.last_ratelimit_state`` is empty: normalise
            #     returned ``{}`` — unmapped provider (Ollama, Gemini
            #     today) or response carried no rate-limit headers at
            #     all. Writing ``{}`` under the provider key would
            #     overwrite a genuine prior snapshot from a sibling
            #     turn with stale "no data" — simpler to not write and
            #     let the previous entry age out naturally.
            #   - SharedKV throws (Redis drop mid-flight, disk-full on
            #     in-memory fallback, JSON-encode edge case): already
            #     handled inside ``set_with_ttl`` via SharedKV's Redis-
            #     best-effort contract, but wrap in an outer try/except
            #     so a bug in the TTL wrapper can't abort the LLM turn.
            try:
                if self.provider and self.last_ratelimit_state:
                    _get_ratelimit_kv().set_with_ttl(
                        self.provider,
                        self.last_ratelimit_state,
                        _RATELIMIT_TTL_SECONDS,
                    )
            except Exception as exc:
                logger.debug(
                    "provider_ratelimit SharedKV write skipped for %s: %s",
                    self.provider or "<unknown>", exc,
                )
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

# ─────────────────────────────────────────────────────────────────
# Z.1 (#290) checkbox 3 (2026-04-24): cross-worker rate-limit state
# store. Each LLM turn's ``on_llm_end`` mirrors
# ``self.last_ratelimit_state`` into this shared hash keyed by provider
# name, so dashboard endpoints + future adaptive-backoff logic
# (Z.2 / Z.4) read the latest snapshot without having to hold a
# reference to the LangChain callback instance.
#
# Key shape intentionally omits tenant / user: the rate-limit is an
# instance-wide property of the single API key every tenant shares,
# so the scope of the information is instance-wide too. Adding tenant
# to the key would produce N copies of the same numbers and confuse
# readers into thinking the limits are tenant-scoped.
#
# TTL = 60 s: rate-limit windows reset ≤ 60 s on all seven mapped
# providers. Stale-ing after that prevents the dashboard from
# surfacing a minute-old "0 remaining" when the provider has already
# refilled. Enforced per-field via ``SharedKV.set_with_ttl`` (embedded
# ``_expires_at`` + lazy prune) so touching one provider's entry
# doesn't inadvertently refresh another's expiry.
#
# Module-global audit (SOP Step 1, 2026-04-21 rule): the ``SharedKV``
# is Redis-backed when ``OMNISIGHT_REDIS_URL`` is set (rubric #2
# "coordinate via Redis" — one hash shared across every uvicorn
# worker + replica); in-memory fallback is per-worker (rubric #3
# "deliberately per-worker" for single-worker dev). No new
# shared-mutable surface beyond what ``SharedKV`` already guarantees.
_RATELIMIT_TTL_SECONDS = 60.0
_RATELIMIT_KV_NAMESPACE = "provider_ratelimit"
_ratelimit_kv_singleton = None


def _get_ratelimit_kv():  # noqa: ANN202
    """Lazy-init + cache the module-level ``SharedKV("provider_ratelimit")``.

    Lazy rather than at import time so ``shared_state.reset_for_tests``
    (which nulls out the Redis clients) does not leave this callback
    holding a stale reference — a fresh ``SharedKV`` instance reopens
    the connection via ``get_sync_redis`` on first use.
    """
    global _ratelimit_kv_singleton
    if _ratelimit_kv_singleton is None:
        from backend.shared_state import SharedKV
        _ratelimit_kv_singleton = SharedKV(_RATELIMIT_KV_NAMESPACE)
    return _ratelimit_kv_singleton


# Cache to avoid re-creating LLM instances
_cache: dict[str, BaseChatModel] = {}
_provider_failures: dict[str, float] = {}  # provider → last_failure_timestamp
PROVIDER_COOLDOWN = 300  # 5 minutes — don't retry a failed provider within this window
_PROVIDER_FAILURES_MAX = 256  # cap to bound memory
_MODEL_MAPPING_MODE_ENV = "OMNISIGHT_MODEL_MAPPING_MODE"
_MODEL_MAPPING_DEFAULT_MODE = "advisory"
_MODEL_MAPPING_MODES = frozenset({"enforce", "warn", "advisory"})

# Lock guards composite read-modify-write on _provider_failures (record +
# prune). CPython single dict ops are atomic, but iteration during prune
# from another thread/coroutine would raise RuntimeError.
import threading as _threading
_provider_failures_lock = _threading.Lock()


def _model_mapping_mode(configured_mode: object | None = None) -> str:
    """Resolve BP.F model-mapping mode from env, config, then default."""
    mode = os.environ.get(_MODEL_MAPPING_MODE_ENV, "").strip().lower()
    if not mode and isinstance(configured_mode, str):
        mode = configured_mode.strip().lower()
    if not mode:
        return _MODEL_MAPPING_DEFAULT_MODE
    if mode not in _MODEL_MAPPING_MODES:
        logger.warning(
            "Unknown %s/config mode=%r; using %s model mapping mode",
            _MODEL_MAPPING_MODE_ENV,
            mode,
            _MODEL_MAPPING_DEFAULT_MODE,
        )
        return _MODEL_MAPPING_DEFAULT_MODE
    return mode


def _model_mapping_guardrail_allows(provider: str, model: str | None) -> bool:
    """Apply BP.F's provider mapping guardrail for one ``get_llm()`` call."""
    try:
        from backend.agents import routing_policy
        import yaml  # pyyaml — already used by routing_policy.

        raw = yaml.safe_load(
            routing_policy._MODEL_MAPPING_PATH.read_text(encoding="utf-8")
        ) or {}
    except OSError:
        return True
    except Exception as exc:
        logger.warning("model mapping guardrail config unavailable: %s", exc)
        return True

    if isinstance(raw, dict):
        raw_providers = raw.get("providers")
        configured_mode = raw.get("mode")
    else:
        raw_providers = {}
        configured_mode = None
    providers = {
        str(item).strip().lower()
        for item in raw_providers
        if isinstance(raw_providers, dict) and str(item).strip()
    }

    if not providers:
        return True

    if not isinstance(provider, str):
        return True
    provider_id = provider.strip().lower()
    if provider_id in providers:
        return True

    mode = _model_mapping_mode(configured_mode)
    message = (
        "LLM provider mapping violation: provider=%r model=%r is absent "
        "from configs/model_mapping.yaml providers=%r"
    )
    args = (provider, model, sorted(providers))
    if mode == "enforce":
        logger.error(message, *args)
        return False
    (logger.warning if mode == "warn" else logger.info)(message, *args)
    return True


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
    *,
    allow_failover: bool = True,
) -> BaseChatModel | None:
    """Create or retrieve a cached LLM instance.

    Args:
        provider: Override the configured provider.
        model: Override the model name.
        bind_tools: Optional list of LangChain tools to bind.
        allow_failover: When ``True`` (default) a failed primary init
            walks ``settings.llm_fallback_chain`` and promotes to the
            first healthy provider — existing wide-blast behaviour for
            all pre-existing callers. When ``False`` the helper returns
            ``None`` if the *specific* (provider, model) pair cannot
            initialise, with no cascade. Used by
            :func:`get_cheapest_model` so a missing DeepSeek key does
            not silently route a utility call to flagship Opus.

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

    if not _model_mapping_guardrail_allows(provider, model):
        return None

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
            if not allow_failover:
                # ZZ.B2 #304-2 checkbox 3: caller opted out of the
                # chain walk (typically ``get_cheapest_model``). Do
                # NOT record a breaker failure — "no API key
                # configured" is an operational state, not a health
                # signal, and flipping the circuit here would poison
                # the legacy cooldown path for later opportunistic
                # calls that DO want the chain.
                return None
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


# ZZ.B2 #304-2 checkbox 3 (2026-04-24): ordered cheapest-first
# preference list for utility LLM calls (auto-title generation, and
# any future short-form summarise/classify helpers). Rates below
# correspond to the per-1M-token pricing in
# ``backend.events._MODEL_PRICING_PER_MTOK`` on 2026-04-24; the order
# additionally honours the user's stated preference (Haiku 4.5 /
# DeepSeek chat / OpenRouter 最便宜).
#
# Each entry is ``(provider, model)``. ``model`` must be an explicit
# string — falling through to the provider default would risk picking
# the provider's flagship (e.g. claude-sonnet-4) and defeating the
# "cheapest-first" guarantee. OpenRouter's aggregator entry is an
# exception: the slash-routed id ``anthropic/claude-haiku-4`` is
# already the cheapest Haiku route through that provider.
#
# Module-global audit (SOP Step 1, 2026-04-21 rule): this is a module-
# const tuple literal — every uvicorn worker derives the identical
# list from the same source (SOP answer #1 "不共享，因為每 worker
# 從同樣來源推導出同樣的值"). No shared mutable state is introduced.
_CHEAPEST_MODEL_PREFERENCE: tuple[tuple[str, str], ...] = (
    ("deepseek", "deepseek-chat"),              # $0.27 / $1.10 per 1M tok
    ("anthropic", "claude-haiku-4-20250506"),   # $0.80 / $4.00 per 1M tok (Haiku 4.5)
    ("openrouter", "anthropic/claude-haiku-4"), # Aggregator fallback — single key
    ("groq", "llama-3.1-8b-instant"),           # Free tier / very cheap + fast
)


def get_cheapest_model(
    bind_tools: list | None = None,
) -> BaseChatModel | None:
    """Return an LLM backed by the cheapest configured provider.

    ZZ.B2 #304-2 checkbox 3: utility LLM calls (auto-title generation
    first, future short-form classifiers second) only spend a handful
    of output tokens per invocation. Routing that traffic through the
    operator-pinned primary (typically Claude Opus) burns flagship
    quota on work that a Haiku / DeepSeek / Groq tier model performs
    equally well at 1–5% of the cost.

    Walks :data:`_CHEAPEST_MODEL_PREFERENCE` in declared order and
    returns the first entry whose provider has credentials + whose
    ``_create_llm`` init succeeds. Uses ``get_llm(..., allow_failover=
    False)`` so a missing DeepSeek key does not silently cascade back
    to Opus and defeat the whole point of the helper.

    If every preference entry fails (fresh install, no keys configured,
    no Ollama), falls back to ``get_llm()`` so the feature still
    works — the caller accepts that its cost guarantee degrades to
    "primary provider's rate" in that edge case rather than silently
    breaking the downstream feature. Callers that cannot tolerate
    that fallback (e.g. a ``token_frozen`` guard) should check the
    returned model's ``.model_name`` attribute.

    ``bind_tools`` is passed through for call-site symmetry with
    :func:`get_llm`; the auto-title helper does not bind tools but
    future summariser callers may.

    Module-global audit: this function is stateless. It consults the
    module-const :data:`_CHEAPEST_MODEL_PREFERENCE` and delegates all
    cache/state to :func:`get_llm` (which already handles the per-
    provider cache + per-tenant circuit breaker correctly).
    """
    for provider, model in _CHEAPEST_MODEL_PREFERENCE:
        llm = get_llm(
            provider=provider,
            model=model,
            bind_tools=bind_tools,
            allow_failover=False,
        )
        if llm is not None:
            logger.debug(
                "get_cheapest_model picked %s / %s", provider, model,
            )
            return llm
    logger.debug(
        "get_cheapest_model preference list exhausted — "
        "falling back to primary get_llm() (cost guarantee degraded)",
    )
    return get_llm(bind_tools=bind_tools)


def _create_llm(provider: str, model: str | None) -> BaseChatModel | None:
    """Instantiate a configured chat model via the adapter firewall.

    All provider-specific instantiation logic (class imports, argument
    shapes, base URLs) lives in `backend.llm_adapter.build_chat_model`.
    This function is now just a resolver-lookup + credential-gate shim.

    Phase 5b-2 (#llm-credentials) — credential read goes through
    :func:`backend.llm_credential_resolver.get_llm_credential_sync`
    instead of ``getattr(settings, f"{provider}_api_key")`` directly.
    The sync resolver reads ``Settings`` (populated from ``.env`` +
    ``backend.llm_secrets.load_into_settings``); row 5b-5's lifespan
    auto-migration will mirror ``llm_credentials`` DB rows into the
    same Settings fields so the sync path stays in lock-step with the
    authoritative table. An unconfigured provider raises
    :class:`LLMCredentialMissingError` which we catch and translate to
    ``None`` so the existing failover cascade in :func:`get_llm`
    continues to work unchanged (primary init → chain walk).
    """
    from backend.llm_credential_resolver import (
        LLMCredentialMissingError,
        get_llm_credential_sync,
    )

    temp = settings.llm_temperature

    # Provider-specific ``build_chat_model`` kwargs that aren't covered
    # by the credential resolver. ``base_url`` for ollama still comes
    # from ``settings.ollama_base_url`` (mirrored into ``metadata.base_url``
    # by the resolver for keyless providers so the adapter has a
    # single shape); OpenRouter ships aggregator-routing headers.
    _PROVIDER_EXTRA_KWARGS: dict[str, dict] = {
        "anthropic": {},
        "google": {},
        "openai": {},
        "xai": {},
        "groq": {},
        "deepseek": {},
        "together": {},
        "openrouter": {
            "default_headers": {
                "HTTP-Referer": "https://omnisight.local",
                "X-Title": "OmniSight Productizer",
            },
        },
        "ollama": {},
    }

    if provider not in _PROVIDER_EXTRA_KWARGS:
        logger.warning("Unknown LLM provider: %s", provider)
        return None

    try:
        cred = get_llm_credential_sync(provider)
    except LLMCredentialMissingError as exc:
        # Keep the historical log shape so operators grepping for
        # "No OMNISIGHT_*_API_KEY set" still hit these lines. The
        # detailed resolver message goes to debug so the failover
        # path's cascade log stays readable at INFO level.
        logger.info("No OMNISIGHT_%s_API_KEY set", provider.upper())
        logger.debug("llm_credential_resolver: %s", exc)
        return None

    # Keyless providers (Ollama) carry an empty api_key; the adapter's
    # ``build_chat_model`` contract accepts ``None`` for those, so swap
    # the empty string back to ``None`` at the boundary.
    api_key = cred.api_key if cred.api_key else None

    extra_kwargs = dict(_PROVIDER_EXTRA_KWARGS[provider])
    if provider == "ollama":
        # ``metadata.base_url`` wins when set (e.g. a future
        # llm_credentials row with a per-tenant ollama endpoint);
        # fallback to the legacy Settings scalar for empty-table
        # deployments where the resolver synthesises a keyless record.
        base_url = cred.metadata.get("base_url") or settings.ollama_base_url
        extra_kwargs["base_url"] = base_url

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


def _load_ollama_tool_calling_compat() -> dict[str, dict]:
    """Load config/ollama_tool_calling.yaml and return a per-model compat dict.

    Loaded per worker and invalidated by file mtime. Returns an empty dict
    when the file is absent or unparseable so list_providers() degrades
    gracefully instead of raising.

    Module-global audit: ``_OLLAMA_TOOL_COMPAT_CACHE`` is per-worker
    process state, keyed by ``config/ollama_tool_calling.yaml`` mtime.
    Every worker derives the same compat matrix from the same shared YAML
    and reloads when another worker/operator updates the file.
    """
    global _OLLAMA_TOOL_COMPAT_CACHE
    try:
        config_mtime = _OLLAMA_TOOL_COMPAT_PATH.stat().st_mtime
    except OSError:
        config_mtime = None
    if (
        _OLLAMA_TOOL_COMPAT_CACHE is not None
        and _OLLAMA_TOOL_COMPAT_CACHE[0] == config_mtime
    ):
        return _OLLAMA_TOOL_COMPAT_CACHE[1]
    import yaml  # pyyaml — already a transitive dep via langchain

    try:
        raw = yaml.safe_load(_OLLAMA_TOOL_COMPAT_PATH.read_text(encoding="utf-8"))
        parsed = raw.get("models", {}) if isinstance(raw, dict) else {}
        _OLLAMA_TOOL_COMPAT_CACHE = (config_mtime, parsed)
        return parsed
    except Exception as exc:  # noqa: BLE001
        logger.warning("ollama_tool_calling.yaml load failed: %s", exc)
    _OLLAMA_TOOL_COMPAT_CACHE = (config_mtime, {})
    return {}


def reload_ollama_tool_calling_compat_for_tests() -> None:
    """Drop the per-worker Ollama compat cache so the next read re-parses YAML.

    Test-only escape hatch. Production code relies on the mtime-based
    invalidation inside :func:`_load_ollama_tool_calling_compat`; tests
    that rewrite the YAML in-place within the same process tick may
    write through to the same mtime second and miss the cache check —
    calling this between writes forces a fresh load.
    """
    global _OLLAMA_TOOL_COMPAT_CACHE
    _OLLAMA_TOOL_COMPAT_CACHE = None


# Populated on first call to _load_ollama_tool_calling_compat(); None means
# "not yet loaded".  Each uvicorn worker populates its own copy from the same
# on-disk file and invalidates when the shared YAML mtime changes.
_OLLAMA_TOOL_COMPAT_CACHE: tuple[float | None, dict[str, dict]] | None = None


def list_providers() -> list[dict]:
    """Return metadata about all supported providers.

    Phase 5b-2 (#llm-credentials): the ``configured`` flag goes through
    :func:`backend.llm_credential_resolver.is_provider_configured`
    rather than reading ``bool(settings.{provider}_api_key)`` directly.
    This keeps the resolver as the single source of truth for
    "does this provider have a credential available"; when row 5b-5's
    auto-migration mirrors DB rows back into Settings, the flag flips
    through the same code path as :func:`get_llm`.
    """
    from backend.llm_credential_resolver import is_provider_configured

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
            "configured": is_provider_configured("anthropic"),
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
            "configured": is_provider_configured("google"),
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
            "configured": is_provider_configured("openai"),
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
            "configured": is_provider_configured("xai"),
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
            "configured": is_provider_configured("groq"),
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
            "configured": is_provider_configured("deepseek"),
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
            "configured": is_provider_configured("together"),
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
            "configured": is_provider_configured("openrouter"),
        },
        {
            "id": "ollama",
            "name": "Ollama (Local)",
            "default_model": "llama3.1",
            # Z.6.4: 9 models confirmed to work with bind_tools() via
            # langchain-ollama >= 0.2 + Ollama 0.3.0+.  See
            # config/ollama_tool_calling.yaml for per-model support levels.
            "models": [
                "llama3.1",
                "llama3.2",
                "qwen2.5",
                "qwen3",
                "mistral-nemo",
                "mistral-small",
                "firefunction-v2",
                "command-r",
                "mixtral",
                # Additional general-purpose models (no guaranteed tool support)
                "mistral",
                "codellama",
                "deepseek-r1",
            ],
            "requires_key": False,
            "env_var": None,
            "configured": is_provider_configured("ollama"),
            "base_url": settings.ollama_base_url,
            # Z.6.4: tool-calling compat matrix from
            # config/ollama_tool_calling.yaml.  Injected here so the frontend
            # can render a badge without a separate API call.  None for all
            # other providers (field is ollama-only).
            "tool_calling_compat": _load_ollama_tool_calling_compat() or None,
        },
    ]
    return providers


def validate_model_spec(model_spec: str) -> dict:
    """Validate a model spec and check whether its provider has credentials.

    Accepts either the explicit ``"<provider>:<model>"`` shape used by
    OpenRouter-style ids (``"openrouter:qwen/qwen3-235b"``) or a bare
    model name (``"claude-sonnet-4"``); for the bare form, walks
    :func:`list_providers` to back-resolve the owning provider via the
    public model whitelist.

    Args:
        model_spec: Model spec — either ``"<provider>:<model>"`` or a
            bare model id appearing in any provider's ``models`` list
            or ``default_model``. Empty string is treated as "no
            override requested" and validates trivially.

    Returns:
        Dict with the following keys (always present):

        - ``valid`` (bool): ``True`` when the spec parses, the provider
          is known, and (if ``requires_key``) a credential is
          configured. ``False`` otherwise.
        - ``provider`` (str): Resolved provider id, or ``""`` when the
          spec was empty.
        - ``model`` (str): Model id (sub-string after the ``":"`` for
          explicit specs, or the raw input for bare names).
        - ``configured`` (bool): Whether the provider currently has a
          credential available (per
          :func:`backend.llm_credential_resolver.is_provider_configured`).
        - ``warning`` (str): Operator-facing explanation when ``valid``
          is ``False`` or when validation passes with a caveat (e.g.
          unknown model falls through to the global default provider).
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

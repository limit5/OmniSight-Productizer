"""Phase 67-A — Prompt cache marker layer.

Lifts the "what is cacheable vs volatile" decision out of every
callsite into a single builder. Producing prompts via
``CachedPromptBuilder`` guarantees three things:

  1. The 5-segment message order contract is enforced at write time:
        system → tools → static_kb → conversation → volatile_log
     The Anthropic cache_control marker only applies to a *prefix*
     of the messages, so getting this order wrong silently destroys
     the cache hit rate.
  2. Per-provider hint injection — Anthropic gets explicit
     ``cache_control: ephemeral``, OpenAI gets nothing (auto-caches
     prompts ≥ 1024 tokens server-side), Ollama gets a one-shot
     warning, unknown providers degrade gracefully.
  3. A canonical hook (`record_cache_outcome`) for caller code to
     report whether the response carried `cache_read_input_tokens`
     so the prompt_cache_hit/miss counters reflect reality, not
     intent.

This module deliberately does NOT call any LLM. It produces
provider-native message lists; the existing `agents/llm.py` adapters
remain authoritative for the actual API call.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

SegmentKind = Literal["system", "tools", "static_kb",
                      "conversation", "volatile_log"]

# Order is the contract. Building a message list out of order rebuilds
# the cache from scratch — defeating the entire point.
_ORDER: tuple[SegmentKind, ...] = (
    "system", "tools", "static_kb", "conversation", "volatile_log",
)

# Segments that should carry the cache hint when the provider supports
# explicit markers. Conversation + volatile_log are deliberately NOT
# cacheable: they change every turn.
_CACHEABLE: frozenset[SegmentKind] = frozenset({
    "system", "tools", "static_kb",
})

# One-shot warning gate per process — no point spamming logs.
_WARNED_PROVIDERS: set[str] = set()


@dataclass
class CacheableSegment:
    kind: SegmentKind
    content: str
    role: str = "user"  # most providers want messages with a role


@dataclass
class CachedPromptBuilder:
    """Collect typed segments, build provider-native messages on demand.

    Typical usage:

        b = CachedPromptBuilder()
        b.add_system("You are an embedded firmware agent.")
        b.add_static_kb(soc_manual_text)
        b.add_conversation(history)
        b.add_volatile_log(latest_compile_output)
        messages = b.build_for("anthropic")
    """

    segments: list[CacheableSegment] = field(default_factory=list)

    # ── Typed adders (enforce order at read time) ──

    def add_system(self, text: str) -> "CachedPromptBuilder":
        self.segments.append(CacheableSegment("system", text, role="system"))
        return self

    def add_tools(self, text: str) -> "CachedPromptBuilder":
        self.segments.append(CacheableSegment("tools", text, role="system"))
        return self

    def add_static_kb(self, text: str) -> "CachedPromptBuilder":
        self.segments.append(CacheableSegment("static_kb", text, role="user"))
        return self

    def add_conversation(self, text: str) -> "CachedPromptBuilder":
        self.segments.append(CacheableSegment("conversation", text, role="user"))
        return self

    def add_volatile_log(self, text: str) -> "CachedPromptBuilder":
        self.segments.append(CacheableSegment("volatile_log", text, role="user"))
        return self

    # ── Build ──

    def _ordered(self) -> list[CacheableSegment]:
        rank = {k: i for i, k in enumerate(_ORDER)}
        # Stable sort: preserve insertion order within the same kind.
        return sorted(self.segments, key=lambda s: rank[s.kind])

    def build_for(self, provider: str) -> list[dict]:
        """Return a list of provider-native message dicts. Empty
        segments (whitespace only) are skipped to avoid wasting tokens
        and to keep the cache-prefix tight."""
        ordered = [s for s in self._ordered() if s.content and s.content.strip()]
        prov = (provider or "").strip().lower()

        if prov == "anthropic":
            return _build_anthropic(ordered)
        if prov == "openai":
            return _build_openai(ordered)
        if prov == "ollama":
            if "ollama" not in _WARNED_PROVIDERS:
                logger.warning(
                    "prompt_cache: Ollama has no explicit cache markers; "
                    "falling back to plain messages. Cache hit rate "
                    "depends on the runtime's own context reuse.",
                )
                _WARNED_PROVIDERS.add("ollama")
            return _build_plain(ordered)
        # Unknown provider — degrade gracefully.
        if prov and prov not in _WARNED_PROVIDERS:
            logger.warning(
                "prompt_cache: provider %r unknown; emitting plain messages",
                prov,
            )
            _WARNED_PROVIDERS.add(prov)
        return _build_plain(ordered)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Provider builders
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_anthropic(ordered: list[CacheableSegment]) -> list[dict]:
    """Anthropic's prompt-caching takes a `cache_control` marker on the
    LAST cacheable content block; everything before it shares the cache
    prefix. We mark every cacheable segment so any rebuild upstream
    (truncating volatile_log first) keeps the cacheable tail intact."""
    out: list[dict] = []
    # Anthropic separates `system` from `messages`. We collapse all
    # system+tools into the system field and treat static_kb /
    # conversation / volatile_log as user-role messages.
    system_chunks = [s for s in ordered if s.kind in ("system", "tools")]
    msg_chunks = [s for s in ordered if s.kind not in ("system", "tools")]

    if system_chunks:
        # Use list-of-blocks so each can carry cache_control.
        sys_blocks = []
        for s in system_chunks:
            sys_blocks.append({
                "type": "text",
                "text": s.content,
                "cache_control": {"type": "ephemeral"},
            })
        out.append({"_anthropic_system_blocks": sys_blocks})

    for s in msg_chunks:
        block: dict = {
            "role": s.role if s.role in ("user", "assistant") else "user",
            "content": [{"type": "text", "text": s.content}],
        }
        if s.kind in _CACHEABLE:
            block["content"][0]["cache_control"] = {"type": "ephemeral"}
        out.append(block)
    return out


def _build_openai(ordered: list[CacheableSegment]) -> list[dict]:
    """OpenAI auto-caches prefixes ≥ 1024 tokens — no explicit marker
    needed. We still preserve the segment order so the prefix stays
    stable across requests (which is what triggers OpenAI's auto-cache)."""
    out: list[dict] = []
    for s in ordered:
        out.append({
            "role": "system" if s.kind in ("system", "tools") else s.role,
            "content": s.content,
        })
    return out


def _build_plain(ordered: list[CacheableSegment]) -> list[dict]:
    """Generic / Ollama / unknown — same as OpenAI shape, no markers.
    Still preserves order so any provider-side prefix cache benefits."""
    return _build_openai(ordered)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Outcome reporting
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def is_enabled() -> bool:
    """Master switch. Default ON; set OMNISIGHT_PROMPT_CACHE_ENABLED=false
    to fall back to plain messages everywhere (debug only — disabling
    this in prod is a regression on cost and TTFT)."""
    val = (os.environ.get("OMNISIGHT_PROMPT_CACHE_ENABLED") or "true").strip().lower()
    return val in {"1", "true", "yes", "on"}


def record_cache_outcome(provider: str, *, hit_tokens: int,
                         miss_tokens: int) -> None:
    """Caller-side hook: after an LLM response, if the SDK exposed
    cache-read vs cache-creation token counts (Anthropic does), feed
    them in. Both args may be 0 — we still record the request as a
    miss so the rate denominator is right."""
    try:
        from backend import metrics as _m
        if hit_tokens > 0:
            _m.prompt_cache_hit_total.labels(provider=provider).inc(hit_tokens)
        else:
            _m.prompt_cache_miss_total.labels(provider=provider).inc(
                miss_tokens or 1,
            )
    except Exception:
        pass


def reset_warnings_for_tests() -> None:
    """Test hook — drop the per-process warned-providers set."""
    _WARNED_PROVIDERS.clear()

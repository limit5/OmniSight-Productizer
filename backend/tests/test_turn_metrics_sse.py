"""ZZ.A2 #303-2 — per-turn `turn_metrics` SSE event regression guards.

Locks the contract between :class:`backend.agents.llm.TokenTrackingCallback`,
:func:`backend.context_limits.get_context_limit`, and
:func:`backend.events.emit_turn_metrics`:

1. **Emit helper payload shape.** ``emit_turn_metrics`` must land the
   canonical 10-field payload on the bus under the ``turn_metrics``
   event type (provider / model / input_tokens / output_tokens /
   tokens_used / context_limit / context_usage_pct / latency_ms /
   cache_read_tokens / cache_create_tokens). Numeric fields are ints;
   ``context_usage_pct`` is a float rounded to 2 decimals.
2. **NULL context_limit degradation.** When ``context_limit`` is
   ``None`` (unknown provider/model, Ollama without the env override,
   OpenRouter pass-through) the helper must emit
   ``context_usage_pct=None`` — NOT ``0.0`` — so the UI can render "—"
   instead of a fabricated zero. This mirrors the ZZ.A1 NULL-vs-
   genuine-zero contract for prompt-cache fields.
3. **Callback end-to-end.** A full ``on_llm_end`` call with a resolved
   provider + model must push a ``turn_metrics`` payload whose
   ``context_usage_pct`` matches ``tokens_used / context_limit * 100``.
4. **Callback back-compat.** Instantiating
   ``TokenTrackingCallback(model_name)`` without a provider (legacy
   tests) must still emit — with ``provider=None`` and
   ``context_limit=None`` + ``context_usage_pct=None`` — so removing
   the callback's optional kwarg in a future refactor is loud.
"""

from __future__ import annotations

import asyncio
import json

import pytest


@pytest.mark.asyncio
async def test_emit_turn_metrics_publishes_canonical_payload():
    from backend import events

    q = events.bus.subscribe()
    try:
        events.emit_turn_metrics(
            "claude-opus-4-7",
            input_tokens=10_000,
            output_tokens=2_000,
            latency_ms=1_234,
            provider="anthropic",
            context_limit=1_000_000,
            cache_read_tokens=500,
            cache_create_tokens=100,
            broadcast_scope="global",
        )
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg["event"] == "turn_metrics"
        payload = json.loads(msg["data"])

        assert payload["provider"] == "anthropic"
        assert payload["model"] == "claude-opus-4-7"
        assert payload["input_tokens"] == 10_000
        assert payload["output_tokens"] == 2_000
        # Convenience sum — frontend bar's numerator.
        assert payload["tokens_used"] == 12_000
        assert payload["context_limit"] == 1_000_000
        # 12_000 / 1_000_000 * 100 = 1.20 (round 2 decimals).
        assert payload["context_usage_pct"] == 1.20
        assert payload["latency_ms"] == 1_234
        assert payload["cache_read_tokens"] == 500
        assert payload["cache_create_tokens"] == 100
        # Bus auto-stamps.
        assert "timestamp" in payload
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_emit_turn_metrics_null_context_limit_degrades_pct():
    """Unknown provider/model → ``context_limit=None`` → pct must be
    ``None`` (not 0). The UI's em-dash rendering depends on this.
    """
    from backend import events

    q = events.bus.subscribe()
    try:
        events.emit_turn_metrics(
            "unknown-local-model",
            input_tokens=4_000,
            output_tokens=1_000,
            latency_ms=500,
            provider="ollama",
            context_limit=None,
            broadcast_scope="global",
        )
        msg = await asyncio.wait_for(q.get(), timeout=1)
        payload = json.loads(msg["data"])

        assert payload["context_limit"] is None
        # Strict: pct is None, not 0 — the whole point of the NULL-vs-
        # genuine-zero contract is that ``None`` stays distinguishable
        # from any real percentage (including 0.0 on a brand-new turn).
        assert payload["context_usage_pct"] is None
        # tokens_used still lands even without a limit — the UI can
        # render the raw count while the pct bar degrades to "—".
        assert payload["tokens_used"] == 5_000
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_emit_turn_metrics_zero_limit_degrades_pct():
    """Defensive: ``context_limit <= 0`` would produce either ZeroDivision
    or a nonsense percentage. Treat the same as ``None`` (render "—").
    """
    from backend import events

    q = events.bus.subscribe()
    try:
        events.emit_turn_metrics(
            "broken-model",
            input_tokens=100,
            output_tokens=50,
            latency_ms=10,
            provider="xai",
            context_limit=0,
            broadcast_scope="global",
        )
        msg = await asyncio.wait_for(q.get(), timeout=1)
        payload = json.loads(msg["data"])
        assert payload["context_usage_pct"] is None
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_callback_on_llm_end_emits_turn_metrics_end_to_end(monkeypatch):
    """Full path: ``TokenTrackingCallback(model, provider=...)`` →
    ``on_llm_end`` → ``get_context_limit`` → ``emit_turn_metrics`` →
    bus. Lock ``context_usage_pct = tokens_used / context_limit * 100``
    with realistic Anthropic numbers so a future refactor of any link
    in the chain breaks loudly.
    """
    from backend import events
    from backend.agents.llm import TokenTrackingCallback

    # Stub ``track_tokens`` — the lifetime-accumulation path is covered
    # by ``test_token_budget.py`` / ``test_shared_state.py``; this test
    # scopes ``on_llm_end → emit_turn_metrics`` alone.
    monkeypatch.setattr(
        "backend.routers.system.track_tokens",
        lambda *args, **kwargs: None,
    )

    class _FakeLLMResult:
        llm_output = {
            "token_usage": {
                "prompt_tokens": 200_000,
                "completion_tokens": 50_000,
                "cache_read_input_tokens": 12_500,
                "cache_creation_input_tokens": 1_200,
            }
        }
        generations: list[list] = [[]]

    cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
    cb.on_llm_start()

    q = events.bus.subscribe()
    try:
        cb.on_llm_end(_FakeLLMResult())
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg["event"] == "turn_metrics"
        payload = json.loads(msg["data"])

        assert payload["provider"] == "anthropic"
        assert payload["model"] == "claude-opus-4-7"
        assert payload["input_tokens"] == 200_000
        assert payload["output_tokens"] == 50_000
        assert payload["tokens_used"] == 250_000
        # claude-opus-4-7 is 1_000_000 in context_window_limits.yaml.
        assert payload["context_limit"] == 1_000_000
        # 250_000 / 1_000_000 * 100 = 25.00
        assert payload["context_usage_pct"] == 25.00
        # Cache counters plumb through from _extract_cache_tokens.
        assert payload["cache_read_tokens"] == 12_500
        assert payload["cache_create_tokens"] == 1_200
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_callback_without_provider_still_emits_with_null_limit(monkeypatch):
    """Backward compatibility: legacy fixtures instantiate
    ``TokenTrackingCallback(model_name)`` without a provider. The callback
    must still emit ``turn_metrics`` — with ``provider=None`` and
    ``context_limit=None`` + ``context_usage_pct=None`` — so removing
    the optional kwarg in the future breaks tests loudly.
    """
    from backend import events
    from backend.agents.llm import TokenTrackingCallback

    monkeypatch.setattr(
        "backend.routers.system.track_tokens",
        lambda *args, **kwargs: None,
    )

    class _FakeLLMResult:
        llm_output = {
            "token_usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
            }
        }
        generations: list[list] = [[]]

    cb = TokenTrackingCallback("some-model")  # legacy signature
    cb.on_llm_start()

    q = events.bus.subscribe()
    try:
        cb.on_llm_end(_FakeLLMResult())
        msg = await asyncio.wait_for(q.get(), timeout=1)
        payload = json.loads(msg["data"])

        assert payload["provider"] is None
        assert payload["model"] == "some-model"
        assert payload["context_limit"] is None
        assert payload["context_usage_pct"] is None
        # Turn tokens still land even without a provider/limit.
        assert payload["tokens_used"] == 150
    finally:
        events.bus.unsubscribe(q)


def test_turn_metrics_registered_in_sse_schema_exports():
    """Drift guard: ``turn_metrics`` must appear in the SSE schema
    registry so the frontend codegen sees it. Catches the common
    "added the emit helper but forgot the registry" footgun.
    """
    from backend.sse_schemas import SSE_EVENT_SCHEMAS, SSETurnMetrics

    assert "turn_metrics" in SSE_EVENT_SCHEMAS
    assert SSE_EVENT_SCHEMAS["turn_metrics"] is SSETurnMetrics
    # Contract fields present on the Pydantic model.
    fields = set(SSETurnMetrics.model_fields.keys())
    assert fields >= {
        "provider", "model", "input_tokens", "output_tokens",
        "tokens_used", "context_limit", "context_usage_pct",
        "latency_ms", "cache_read_tokens", "cache_create_tokens",
    }

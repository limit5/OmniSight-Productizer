"""ZZ.B1 #304-1 checkbox 3 — per-turn ``turn.complete`` SSE regression guards.

Locks the contract between :class:`backend.agents.llm.TokenTrackingCallback`,
:func:`backend.events.emit_turn_complete`, and the persisted-events
allow-list (``_PERSIST_EVENT_TYPES``):

1. **emit_turn_complete payload shape** — canonical fields land on the
   bus under the ``turn.complete`` event type with messages + tools +
   backend-authoritative cost. Numeric fields are ints; cost is a float
   or ``None`` (NULL-vs-genuine-zero — unknown models propagate
   ``cost_usd=None`` so the UI renders ``$—``).
2. **Persistence** — ``turn.complete`` is in ``_PERSIST_EVENT_TYPES`` so
   ``GET /runtime/turns`` can backfill history.
3. **Callback end-to-end** — ``TokenTrackingCallback.on_chat_model_start``
   captures prompt messages; ``on_llm_end`` appends the assistant
   response and emits ``turn.complete`` with the full conversation.
4. **Schema registry** — ``turn.complete`` is registered in
   ``SSE_EVENT_SCHEMAS`` so the frontend codegen sees it.
"""

from __future__ import annotations

import asyncio
import json

import pytest


@pytest.mark.asyncio
async def test_emit_turn_complete_publishes_canonical_payload():
    from backend import events

    q = events.bus.subscribe()
    try:
        events.emit_turn_complete(
            turn_id="turn-abc123",
            model="claude-opus-4-7",
            input_tokens=5_200,
            output_tokens=2_100,
            latency_ms=350,
            provider="anthropic",
            context_limit=1_000_000,
            cache_read_tokens=12_000,
            cache_create_tokens=4_000,
            messages=[
                {"role": "system", "content": "you are helpful"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there!"},
            ],
            tool_calls=[
                {"name": "run_bash", "success": True,
                 "args": {"cmd": "ls"}, "result": "a\nb\n", "duration_ms": 12},
                {"name": "web_fetch", "success": False,
                 "result": "timeout"},
            ],
            agent_type="orchestrator",
            task_id="task-42",
            started_at="2026-04-24T00:00:00Z",
            ended_at="2026-04-24T00:00:00.350Z",
            summary="hi there!",
            broadcast_scope="global",
        )
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg["event"] == "turn.complete"
        payload = json.loads(msg["data"])

        assert payload["turn_id"] == "turn-abc123"
        assert payload["provider"] == "anthropic"
        assert payload["model"] == "claude-opus-4-7"
        assert payload["input_tokens"] == 5_200
        assert payload["output_tokens"] == 2_100
        assert payload["tokens_used"] == 7_300
        assert payload["context_limit"] == 1_000_000
        assert payload["context_usage_pct"] == 0.73  # 7300/1M*100
        assert payload["latency_ms"] == 350
        assert payload["cache_read_tokens"] == 12_000
        assert payload["cache_create_tokens"] == 4_000
        # 5200/1M*15 + 2100/1M*75 = 0.078 + 0.1575 = 0.2355
        assert payload["cost_usd"] is not None
        assert abs(payload["cost_usd"] - 0.2355) < 1e-4
        assert payload["agent_type"] == "orchestrator"
        assert payload["task_id"] == "task-42"
        assert payload["started_at"] == "2026-04-24T00:00:00Z"
        assert payload["ended_at"] == "2026-04-24T00:00:00.350Z"
        assert payload["summary"] == "hi there!"
        assert len(payload["messages"]) == 3
        assert [m["role"] for m in payload["messages"]] == ["system", "user", "assistant"]
        assert payload["tool_call_count"] == 2
        assert payload["tool_failure_count"] == 1  # web_fetch failed
        assert payload["tool_calls"][0]["name"] == "run_bash"
        assert payload["tool_calls"][0]["success"] is True
        assert payload["tool_calls"][1]["success"] is False
        assert "timestamp" in payload
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_emit_turn_complete_unknown_model_null_cost():
    """NULL-vs-genuine-zero: unknown models must return ``cost_usd=None``
    (not 0.0), mirroring the ``context_usage_pct=None`` contract for
    unknown providers. The UI distinguishes "no pricing data" from
    "this turn was free".
    """
    from backend import events

    q = events.bus.subscribe()
    try:
        events.emit_turn_complete(
            turn_id="turn-unk",
            model="custom-mystery-model-9000",
            input_tokens=100,
            output_tokens=50,
            latency_ms=10,
            provider="custom",
            context_limit=None,
            broadcast_scope="global",
        )
        msg = await asyncio.wait_for(q.get(), timeout=1)
        payload = json.loads(msg["data"])
        assert payload["cost_usd"] is None
        assert payload["context_usage_pct"] is None
        # Empty messages / tools still land as [] so frontend can rely
        # on the shape without a hasOwnProperty dance.
        assert payload["messages"] == []
        assert payload["tool_calls"] == []
        assert payload["tool_call_count"] == 0
        assert payload["tool_failure_count"] == 0
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_emit_turn_complete_local_model_zero_cost_not_null():
    """Local / free models (ollama, llama, gemma) must return
    ``cost_usd=0.0`` (not ``None``) — they're genuinely free,
    distinguishable from "unknown pricing".
    """
    from backend import events

    q = events.bus.subscribe()
    try:
        events.emit_turn_complete(
            turn_id="turn-local",
            model="gemma4:e4b",
            input_tokens=10_000,
            output_tokens=2_000,
            latency_ms=50,
            provider="ollama",
            broadcast_scope="global",
        )
        msg = await asyncio.wait_for(q.get(), timeout=1)
        payload = json.loads(msg["data"])
        assert payload["cost_usd"] == 0.0
    finally:
        events.bus.unsubscribe(q)


def test_turn_complete_is_persisted_event_type():
    """Drift guard: ``turn.complete`` must be in the persist allow-list
    so ``GET /runtime/turns`` can backfill history from event_log.
    """
    from backend.events import _PERSIST_EVENT_TYPES
    assert "turn.complete" in _PERSIST_EVENT_TYPES


def test_turn_complete_registered_in_sse_schema_exports():
    """Drift guard: ``turn.complete`` must appear in SSE_EVENT_SCHEMAS
    so the frontend codegen sees the payload shape.
    """
    from backend.sse_schemas import SSE_EVENT_SCHEMAS, SSETurnComplete
    assert "turn.complete" in SSE_EVENT_SCHEMAS
    assert SSE_EVENT_SCHEMAS["turn.complete"] is SSETurnComplete
    fields = set(SSETurnComplete.model_fields.keys())
    # Contract: at minimum these fields must be on the Pydantic model.
    assert fields >= {
        "turn_id", "provider", "model", "input_tokens", "output_tokens",
        "tokens_used", "latency_ms", "cache_read_tokens",
        "cache_create_tokens", "cost_usd", "messages", "tool_calls",
        "tool_call_count", "tool_failure_count",
    }


@pytest.mark.asyncio
async def test_callback_on_llm_end_emits_turn_complete_with_messages(monkeypatch):
    """Full path: ``TokenTrackingCallback(model, provider=...)`` →
    ``on_chat_model_start`` captures prompt → ``on_llm_end`` appends
    assistant response → ``emit_turn_complete`` fires with the full
    conversation chain on the bus. Also locks the ordering: the
    ``turn_metrics`` emit precedes ``turn.complete`` so the frontend
    ring buffer materialises a bare card *before* the drawer-worthy
    details arrive.
    """
    from backend import events
    from backend.agents.llm import TokenTrackingCallback

    monkeypatch.setattr(
        "backend.routers.system.track_tokens",
        lambda *args, **kwargs: None,
    )

    # Duck-typed LangChain message shims — TokenTrackingCallback only
    # reads ``.type`` / ``.content`` / ``.name`` so we don't need the
    # real BaseMessage classes (would require importing through the
    # adapter firewall which these unit tests skip for speed).
    class _Msg:
        def __init__(self, typ: str, content: str, name: str | None = None) -> None:
            self.type = typ
            self.content = content
            self.name = name

    class _Gen:
        def __init__(self, msg: _Msg) -> None:
            self.message = msg
            self.text = msg.content

    class _FakeLLMResult:
        def __init__(self) -> None:
            self.llm_output = {
                "token_usage": {
                    "prompt_tokens": 200,
                    "completion_tokens": 50,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 10,
                }
            }
            self.generations = [[_Gen(_Msg("ai", "certainly — here is the answer."))]]

    cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")

    # Simulate the LangChain chat-model lifecycle: start emits prompt
    # messages, end emits usage + assistant response.
    cb.on_chat_model_start(
        serialized=None,
        messages=[[
            _Msg("system", "you are a coding assistant"),
            _Msg("human", "what is 2+2?"),
        ]],
    )

    q = events.bus.subscribe()
    try:
        cb.on_llm_end(_FakeLLMResult())

        # Two emits expected: turn_metrics first, then turn.complete.
        first = await asyncio.wait_for(q.get(), timeout=1)
        second = await asyncio.wait_for(q.get(), timeout=1)
        assert first["event"] == "turn_metrics"
        assert second["event"] == "turn.complete"

        payload = json.loads(second["data"])
        assert payload["provider"] == "anthropic"
        assert payload["model"] == "claude-opus-4-7"
        assert payload["input_tokens"] == 200
        assert payload["output_tokens"] == 50
        assert payload["cache_read_tokens"] == 50
        assert payload["cache_create_tokens"] == 10
        assert payload["cost_usd"] is not None  # claude-opus prefix matches
        # Messages — 2 prompt + 1 assistant response.
        roles = [m["role"] for m in payload["messages"]]
        assert roles == ["system", "user", "assistant"]
        assert payload["messages"][0]["content"] == "you are a coding assistant"
        assert payload["messages"][2]["content"] == "certainly — here is the answer."
        # Summary is a truncated slice of the assistant content.
        assert payload["summary"] is not None
        assert payload["summary"].startswith("certainly")
        # turn_id is a non-empty string; shape is stable ("turn-<hex>").
        assert isinstance(payload["turn_id"], str)
        assert payload["turn_id"].startswith("turn-")
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_on_llm_start_clears_stale_prompt_messages():
    """Non-chat completion models invoke ``on_llm_start`` (not
    ``on_chat_model_start``). The callback must clear any stale prompt
    messages from a prior chat turn so a ``turn.complete`` for the
    non-chat call doesn't leak the prior session's system prompt.
    """
    from backend.agents.llm import TokenTrackingCallback

    cb = TokenTrackingCallback("some-model")
    cb._prompt_messages = [{"role": "system", "content": "stale"}]
    cb.on_llm_start()
    assert cb._prompt_messages == []

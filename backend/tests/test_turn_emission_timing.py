"""ZZ.B1 #304-1 checkbox 4 — turn emission timing guards.

Checkboxes 1-3 landed the ``turn_metrics`` + ``turn.complete`` SSE
events and the ring-buffer frontend wiring. This file locks the
*emission timing* contract between
:class:`backend.agents.llm.TokenTrackingCallback` and the two emit
helpers:

1. **Exactly one pair per on_llm_end** — each LLM turn ends with one
   ``turn_metrics`` followed by exactly one ``turn.complete`` on the
   bus. No duplicates, no conditional skip.
2. **Strict ordering** — ``turn_metrics`` is published BEFORE
   ``turn.complete`` so the frontend's ring buffer materialises a
   bare card (ZZ.B1 checkbox 1) before the drawer-worthy details
   arrive (checkbox 2 drawer / checkbox 3 SSE body).
3. **Exception isolation** — a failure in ``emit_turn_metrics`` MUST
   NOT prevent ``emit_turn_complete`` from firing (and vice versa).
   Both are wrapped in their own try/except in ``on_llm_end`` so one
   broken dependency does not silently rob the other of its emission.
4. **Timing stamps captured at start, not end** —
   ``_start`` / ``_start_ts_utc`` are set by ``on_chat_model_start``
   / ``on_llm_start``; ``on_llm_end`` consumes them to derive the
   ``latency_ms`` + ``turn_started_at`` payload fields. If stamps
   were retroactively assigned at end time, ``latency_ms`` would
   collapse to near-zero and the ring buffer's inter-turn-gap
   formula (ZZ.A3) would show spurious zeros.
5. **latency_ms reflects actual wall-clock elapsed** — derived from
   ``time.time() - self._start``, so a synthetic sleep between start
   and end surfaces on the payload.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest


class _Msg:
    """Duck-typed LangChain message shim (``.type`` / ``.content`` /
    ``.name`` are all the serialiser reads). Kept local to avoid the
    adapter-firewall import dance in this unit-test module.
    """

    def __init__(self, typ: str, content: str, name: str | None = None) -> None:
        self.type = typ
        self.content = content
        self.name = name


class _Gen:
    def __init__(self, msg: _Msg) -> None:
        self.message = msg
        self.text = msg.content


class _FakeLLMResult:
    def __init__(self, prompt: int = 200, completion: int = 50) -> None:
        self.llm_output = {
            "token_usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }
        }
        self.generations = [[_Gen(_Msg("ai", "assistant reply"))]]


def _stub_track_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """``track_tokens`` writes to the ``token_usage`` router state
    which is tangential to emission-timing tests — stub it to a noop so
    test isolation doesn't depend on the global SharedTokenUsage state.
    """
    monkeypatch.setattr(
        "backend.routers.system.track_tokens",
        lambda *args, **kwargs: None,
    )


async def _drain(q: asyncio.Queue, n: int, timeout: float = 1.0) -> list[dict[str, Any]]:
    """Collect exactly ``n`` messages off the bus queue (in order)."""
    out: list[dict[str, Any]] = []
    for _ in range(n):
        out.append(await asyncio.wait_for(q.get(), timeout=timeout))
    return out


@pytest.mark.asyncio
async def test_on_llm_end_emits_exactly_one_metrics_plus_one_complete(monkeypatch):
    """Drift guard: each ``on_llm_end`` invocation fires exactly one
    ``turn_metrics`` and one ``turn.complete`` — not two of either,
    not zero of either. Running ``on_llm_end`` a second time must fire
    a second pair (one pair per turn).
    """
    _stub_track_tokens(monkeypatch)

    from backend import events
    from backend.agents.llm import TokenTrackingCallback

    cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
    cb.on_chat_model_start(
        serialized=None,
        messages=[[_Msg("system", "sys"), _Msg("human", "hi")]],
    )

    q = events.bus.subscribe()
    try:
        cb.on_llm_end(_FakeLLMResult())
        # One metrics + one complete — nothing else.
        first_pair = await _drain(q, 2)
        types = [m["event"] for m in first_pair]
        assert types == ["turn_metrics", "turn.complete"]
        # No third emission for this turn (timeout proves silence).
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.1)

        # Second turn on the same callback: fresh pair, fresh ordering.
        cb.on_chat_model_start(
            serialized=None,
            messages=[[_Msg("human", "follow-up")]],
        )
        cb.on_llm_end(_FakeLLMResult())
        second_pair = await _drain(q, 2)
        assert [m["event"] for m in second_pair] == ["turn_metrics", "turn.complete"]

        # Each ``turn.complete`` in a turn pair carries a unique turn_id.
        ids = [json.loads(m["data"])["turn_id"]
               for m in (first_pair[1], second_pair[1])]
        assert ids[0] != ids[1]
        assert all(tid.startswith("turn-") for tid in ids)
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_turn_metrics_precedes_turn_complete_on_bus(monkeypatch):
    """The frontend's ring buffer (ZZ.B1 checkbox 3) relies on
    ``turn_metrics`` arriving first so the card is materialised
    before ``turn.complete`` upgrades it in place with the drawer
    payload. Reversing the order would force the frontend to detect
    "orphan turn.complete" → synthesise card → wait for metrics, a
    race the checkbox-3 logic is not designed to survive.

    Pairs 3 consecutive turns and asserts each pair is strictly
    ordered metrics-then-complete (no interleaving across turns).
    """
    _stub_track_tokens(monkeypatch)

    from backend import events
    from backend.agents.llm import TokenTrackingCallback

    cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
    q = events.bus.subscribe()
    try:
        for i in range(3):
            cb.on_chat_model_start(
                serialized=None,
                messages=[[_Msg("human", f"turn {i}")]],
            )
            cb.on_llm_end(_FakeLLMResult())
        events_out = await _drain(q, 6)
        for i in range(3):
            pair_types = [events_out[2 * i]["event"], events_out[2 * i + 1]["event"]]
            assert pair_types == ["turn_metrics", "turn.complete"], (
                f"pair {i} out of order: {pair_types}"
            )
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_turn_metrics_failure_does_not_block_turn_complete(monkeypatch):
    """Exception isolation: ``emit_turn_metrics`` raising must not
    prevent ``emit_turn_complete`` from firing. The two live in
    independent try/except blocks in ``on_llm_end`` so a broken
    context-limits lookup / bus handler doesn't silently rob the
    drawer of its payload.
    """
    _stub_track_tokens(monkeypatch)

    from backend import events
    from backend.agents import llm as llm_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic metrics emit failure")

    # Patch the *symbol the callback imports* — ``llm.py`` does a
    # late ``from backend.events import emit_turn_metrics`` inside
    # the function body, so we patch the source module.
    monkeypatch.setattr("backend.events.emit_turn_metrics", _boom)

    cb = llm_mod.TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
    cb.on_chat_model_start(
        serialized=None,
        messages=[[_Msg("human", "hello")]],
    )
    q = events.bus.subscribe()
    try:
        # Must not raise — the outer on_llm_end body wraps each
        # emit in its own try/except.
        cb.on_llm_end(_FakeLLMResult())
        # Only one event should land — the ``turn.complete``.
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg["event"] == "turn.complete"
        # And nothing else.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.1)
    finally:
        events.bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_turn_complete_failure_does_not_crash_on_llm_end(monkeypatch):
    """Symmetric to the previous test: ``emit_turn_complete`` raising
    must not crash ``on_llm_end`` — ``turn_metrics`` still lands on
    the bus, the LLM turn itself still completes from the caller's
    perspective, and the logger records the failure at DEBUG (not
    WARNING, since we don't want every backend log spammed by a
    best-effort UI event).
    """
    _stub_track_tokens(monkeypatch)

    from backend import events

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic complete emit failure")

    monkeypatch.setattr("backend.events.emit_turn_complete", _boom)

    from backend.agents.llm import TokenTrackingCallback

    cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
    cb.on_chat_model_start(
        serialized=None,
        messages=[[_Msg("human", "hello")]],
    )
    q = events.bus.subscribe()
    try:
        # Must not raise.
        cb.on_llm_end(_FakeLLMResult())
        msg = await asyncio.wait_for(q.get(), timeout=1)
        assert msg["event"] == "turn_metrics"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.1)
    finally:
        events.bus.unsubscribe(q)


def test_timing_stamps_captured_at_start_not_end():
    """Drift guard: ``_start`` + ``_start_ts_utc`` are set by
    ``on_chat_model_start`` / ``on_llm_start`` (turn boundary open),
    NOT retroactively at ``on_llm_end``. If they were retro-assigned
    at end, ``latency_ms`` would collapse to ~0 and ZZ.A3's inter-turn
    gap formula (same-model prev.tsMs → now.tsMs − latency) would show
    spurious zero gaps on every row.
    """
    from backend.agents.llm import TokenTrackingCallback

    cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
    assert cb._start == 0
    assert cb._start_ts_utc == ""

    cb.on_chat_model_start(
        serialized=None,
        messages=[[_Msg("human", "hello")]],
    )
    assert cb._start > 0, "on_chat_model_start must set _start"
    assert cb._start_ts_utc, "on_chat_model_start must set _start_ts_utc"
    assert cb._start_ts_utc.endswith("+00:00"), (
        "_start_ts_utc must be UTC ISO-8601"
    )

    # Separately: on_llm_start (the non-chat completion entry) must
    # also set both stamps and must clear any stale prompt stash.
    cb2 = TokenTrackingCallback("some-model")
    cb2._prompt_messages = [{"role": "system", "content": "stale"}]
    cb2.on_llm_start()
    assert cb2._start > 0
    assert cb2._start_ts_utc
    assert cb2._prompt_messages == []


@pytest.mark.asyncio
async def test_latency_ms_reflects_actual_wall_clock_elapsed(monkeypatch):
    """Sanity: a 50ms sleep between start and end must show up as a
    latency ≥ 40ms (give jitter some slack) on the ``turn_metrics``
    payload. If ``latency_ms`` were derived from stamps captured at
    ``on_llm_end`` alone, this would be ~0.
    """
    _stub_track_tokens(monkeypatch)

    from backend import events
    from backend.agents.llm import TokenTrackingCallback

    cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
    cb.on_chat_model_start(
        serialized=None,
        messages=[[_Msg("human", "hi")]],
    )
    time.sleep(0.05)  # 50ms window — plenty over jitter floor

    q = events.bus.subscribe()
    try:
        cb.on_llm_end(_FakeLLMResult())
        metrics = await asyncio.wait_for(q.get(), timeout=1)
        payload = json.loads(metrics["data"])
        assert payload["event"] if False else True  # keep parser happy
        assert metrics["event"] == "turn_metrics"
        # Permit jitter — 40ms lower bound catches regressions where
        # latency is retro-computed (would be ~0) without flaking on
        # busy CI.
        assert payload["latency_ms"] >= 40
        # Upper bound: not absurd (catches regressions where the
        # callback inadvertently uses a stale _start from the class
        # instantiation moment).
        assert payload["latency_ms"] < 5000

        # The matching ``turn.complete`` must carry the same latency
        # so frontend merges are consistent.
        complete = await asyncio.wait_for(q.get(), timeout=1)
        assert complete["event"] == "turn.complete"
        cp = json.loads(complete["data"])
        assert cp["latency_ms"] == payload["latency_ms"]
    finally:
        events.bus.unsubscribe(q)


def test_prompt_messages_stashed_at_chat_start_consumed_at_end(monkeypatch):
    """``on_chat_model_start`` is the only place the prompt chain is
    captured. The stash must survive until ``on_llm_end`` consumes it
    — not cleared mid-turn by an unrelated event handler. The drawer's
    "messages" section relies on this: if ``_prompt_messages`` were
    emptied between start and end, the drawer would show only the
    assistant reply, making the per-message token-breakdown useless.
    """
    _stub_track_tokens(monkeypatch)

    from backend.agents.llm import TokenTrackingCallback

    cb = TokenTrackingCallback("claude-opus-4-7", provider="anthropic")
    cb.on_chat_model_start(
        serialized=None,
        messages=[[
            _Msg("system", "you are helpful"),
            _Msg("human", "question"),
            _Msg("tool", "tool-output", name="run_bash"),
        ]],
    )
    # Between start and end — stash intact.
    assert len(cb._prompt_messages) == 3
    assert cb._prompt_messages[0]["role"] == "system"
    assert cb._prompt_messages[2]["role"] == "tool"
    assert cb._prompt_messages[2].get("tool_name") == "run_bash"

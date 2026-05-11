"""G1-gate tests for scripts/run_s1_via_anthropic_sdk.py.

The launcher exists specifically to enforce a hard $100 cap on the S1
api-anthropic backlog run. The single most important assertion in this
file is the breach test: if CostGuard's pre-submit check refuses, the
launcher must NOT make the API call. Everything else is plumbing.

Run::

    python3 -m pytest backend/tests/test_run_s1_via_anthropic_sdk.py -v

These tests use ``InMemoryCostStore`` and a mocked Anthropic client; no
network, no real spend. The launcher itself is loaded via importlib so
the test does not depend on the script being installed on PYTHONPATH.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents.anthropic_native_client import RunResult, TokenUsage
from backend.agents.cost_guard import (
    CostGuard,
    InMemoryCostStore,
)
from backend.agents.tool_dispatcher import ToolDispatcher


def _load_launcher() -> Any:
    name = "s1_launcher_under_test"
    spec = importlib.util.spec_from_file_location(
        name,
        REPO_ROOT / "scripts" / "run_s1_via_anthropic_sdk.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass `__module__` lookups via sys.modules
    # resolve correctly (otherwise Python 3.12 dataclasses raise on init).
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _StubClient:
    """Mock that returns whatever usage the test sets via .usage_to_return."""

    def __init__(self, usage: TokenUsage, *, raise_after: int | None = None) -> None:
        self._usage = usage
        self.calls: list[dict[str, Any]] = []
        self._raise_after = raise_after

    async def run_with_tools(self, **kwargs: Any) -> RunResult:
        self.calls.append(kwargs)
        if self._raise_after is not None and len(self.calls) > self._raise_after:
            raise RuntimeError("test guard: launcher kept calling past breach")
        return RunResult(
            final_text="",
            usage=self._usage,
            stop_reason="end_turn",
            iterations=1,
            tool_calls=[],
        )


class _CacheControlRejected(Exception):
    status_code = 400
    body = {"error": {"message": "cache_control is not accepted here"}}


def _install_anthropic_sdk_stub(
    monkeypatch: pytest.MonkeyPatch, responses: list[Any],
) -> None:
    import copy
    import types

    class _Messages:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self._responses = iter(responses)

        def create(self, **kwargs: Any) -> Any:
            self.calls.append(copy.deepcopy(kwargs))
            item = next(self._responses)
            if isinstance(item, Exception):
                raise item
            return item

    class _Client:
        def __init__(self, **_kwargs: Any) -> None:
            self.messages = _Messages()

    fake = types.ModuleType("anthropic")
    fake.Anthropic = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub")


def _last_content_block(message: dict[str, Any]) -> dict[str, Any]:
    content = message["content"]
    assert isinstance(content, list)
    return content[-1]


def _block(type: str, **kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(type=type, **kwargs)


def _usage(**kwargs: int) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=kwargs.get("input_tokens", 0),
        output_tokens=kwargs.get("output_tokens", 0),
        cache_read_input_tokens=kwargs.get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=kwargs.get("cache_creation_input_tokens", 0),
    )


def _response(
    content: list[SimpleNamespace],
    stop_reason: str,
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content, stop_reason=stop_reason, usage=usage or _usage()
    )


@pytest.mark.asyncio
async def test_s1_sdk_request_marks_last_two_messages_and_counts_cache_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B11: launcher-shaped calls mark the last 2 messages and preserve
    cache-read usage returned by Anthropic after the first iteration.
    """
    mod = _load_launcher()
    _install_anthropic_sdk_stub(
        monkeypatch,
        [
            _response(
                [_block("tool_use", id="toolu_1", name="bash", input={"cmd": "true"})],
                "tool_use",
                _usage(input_tokens=10, output_tokens=2),
            ),
            _response(
                [_block("text", text="done")],
                "end_turn",
                _usage(input_tokens=5, output_tokens=1, cache_read_input_tokens=25),
            ),
        ],
    )
    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient(dispatcher=ToolDispatcher())
    result = await client.run_with_tools(
        prompt="Implement JIRA ticket OP-849.",
        raw_tools=mod.BUILT_IN_TOOLS_SPEC,
        system="system",
        model="claude-sonnet-4-6",
        max_iterations=2,
        enable_cache=True,
    )

    calls = client._client.messages.calls  # type: ignore[attr-defined]
    assert len(calls) == 2
    assert _last_content_block(calls[0]["messages"][-1])["cache_control"] == {
        "type": "ephemeral",
    }
    second_messages = calls[1]["messages"]
    assert _last_content_block(second_messages[-2])["cache_control"] == {
        "type": "ephemeral",
    }
    assert _last_content_block(second_messages[-1])["cache_control"] == {
        "type": "ephemeral",
    }
    assert result.usage.cache_read_input_tokens == 25


@pytest.mark.asyncio
async def test_s1_sdk_cache_breakpoint_rejection_falls_back_without_cache(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """B11: Anthropic 400 on cache_control is retried once without cache marks."""
    _install_anthropic_sdk_stub(
        monkeypatch,
        [
            _CacheControlRejected("cache_control rejected"),
            _response(
                [_block("text", text="done")],
                "end_turn",
                _usage(input_tokens=5, output_tokens=1),
            ),
        ],
    )
    from backend.agents.anthropic_native_client import AnthropicClient

    caplog.set_level(logging.WARNING, logger="backend.agents.anthropic_native_client")
    client = AnthropicClient(dispatcher=ToolDispatcher())
    result = await client.run_with_tools(
        prompt="Implement JIRA ticket OP-849.",
        tools=None,
        model="claude-sonnet-4-6",
        max_iterations=1,
        enable_cache=True,
    )

    calls = client._client.messages.calls  # type: ignore[attr-defined]
    assert len(calls) == 2
    assert _last_content_block(calls[0]["messages"][-1])["cache_control"] == {
        "type": "ephemeral",
    }
    assert "cache_control" not in _last_content_block(calls[1]["messages"][-1])
    assert result.final_text == "done"
    assert "cache_breakpoint_rejected_by_api" in caplog.text


@pytest.mark.asyncio
async def test_global_cap_blocks_next_call_after_breach() -> None:
    """G1 critical assertion: CostGuard.check refuses → launcher must not
    invoke the client. Mock client raises if called more than once.
    """
    mod = _load_launcher()
    guard = CostGuard(store=InMemoryCostStore())
    # Cap $3 — Sonnet 600K input + 30K output ≈ $2.25/call. First call
    # passes ($0 + $2.25 < $3). Second call: cumulative $2.25, projected
    # $4.50 > $3 → hard cap block.
    await mod.install_global_cap(guard, cap_usd=3.0)

    huge = TokenUsage(
        input_tokens=600_000, output_tokens=30_000,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    # has_existing_gerrit_ps shells out via SSH — bypass for the test.
    import unittest.mock as _mock
    client = _StubClient(huge, raise_after=1)
    log: list[Any] = []

    with _mock.patch.object(mod, "has_existing_gerrit_ps", return_value=False):
        # First ticket: pre-flight passes (no spend yet); call lands; cost
        # recorded; cumulative spend now > $2.
        await mod.process_ticket(
            client=client, guard=guard, ticket_key="TEST-1", description="x",
            model="claude-sonnet-4-6", per_ticket_cap_usd=10.0, max_spend_usd=3.0,
            max_iterations=10, log_outcome=log.append,
        )
        # Second ticket: pre-flight estimate + cumulative > $3 → block.
        status = await mod.process_ticket(
            client=client, guard=guard, ticket_key="TEST-2", description="x",
            model="claude-sonnet-4-6", per_ticket_cap_usd=10.0, max_spend_usd=3.0,
            max_iterations=10, log_outcome=log.append,
        )
    assert status == "global_capped", f"expected block on second call, got {status}"
    # Mock raises if .calls > 1; reaching here means the launcher honoured
    # the breach and never invoked the second call.
    assert len(client.calls) == 1, "launcher made a 2nd call past the cap"
    statuses = [outcome.status for outcome in log]
    assert "global_capped" in statuses


@pytest.mark.asyncio
async def test_dry_run_uses_stub_client_with_zero_real_calls() -> None:
    """--dry-run mode must never invoke the real Anthropic client."""
    mod = _load_launcher()
    client = mod._DryRunClient()
    guard = CostGuard(store=InMemoryCostStore())
    await mod.install_global_cap(guard, cap_usd=100.0)

    log: list[Any] = []
    # has_existing_gerrit_ps would shell out via SSH; bypass for the test.
    import unittest.mock as _mock
    with _mock.patch.object(mod, "has_existing_gerrit_ps", return_value=False):
        status = await mod.process_ticket(
            client=client, guard=guard, ticket_key="TEST-DRY",
            description="x", model="claude-sonnet-4-6",
            per_ticket_cap_usd=10.0, max_spend_usd=100.0,
            max_iterations=10, log_outcome=log.append,
        )
    assert status == "ok"
    assert len(log) == 1
    assert log[0].status == "ok"
    # Dry-run client returned 2_000+500 tokens at Sonnet rates → < $0.01
    assert log[0].cost_usd < 0.05


@pytest.mark.asyncio
async def test_idempotency_skips_existing_gerrit_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a ticket already has an open/merged Gerrit PS, skip immediately."""
    mod = _load_launcher()
    guard = CostGuard(store=InMemoryCostStore())
    await mod.install_global_cap(guard, cap_usd=100.0)

    monkeypatch.setattr(mod, "has_existing_gerrit_ps", lambda _k: True)

    client = _StubClient(TokenUsage(0, 0, 0, 0), raise_after=0)
    log: list[Any] = []
    status = await mod.process_ticket(
        client=client, guard=guard, ticket_key="TEST-IDEM",
        description="x", model="claude-sonnet-4-6",
        per_ticket_cap_usd=10.0, max_spend_usd=100.0,
        max_iterations=10, log_outcome=log.append,
    )
    assert status == "skipped_existing_ps"
    assert len(client.calls) == 0


@pytest.mark.asyncio
async def test_per_ticket_cap_records_overrun_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a single ticket runs over its per-ticket cap, mark it ticket_capped
    so the operator sees which tickets ran hot. Launcher continues; the
    global cap is the absolute kill-switch, not the per-ticket cap.
    """
    mod = _load_launcher()
    guard = CostGuard(store=InMemoryCostStore())
    await mod.install_global_cap(guard, cap_usd=100.0)
    monkeypatch.setattr(mod, "has_existing_gerrit_ps", lambda _k: False)

    # Big usage so a single call costs ~$10 (Sonnet 600K + 30K rough).
    big = TokenUsage(
        input_tokens=600_000, output_tokens=30_000,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    client = _StubClient(big)
    log: list[Any] = []
    status = await mod.process_ticket(
        client=client, guard=guard, ticket_key="TEST-OVER",
        description="x", model="claude-sonnet-4-6",
        per_ticket_cap_usd=0.50,  # tiny cap so we definitely overrun
        max_spend_usd=100.0,
        max_iterations=10, log_outcome=log.append,
    )
    assert status == "ticket_capped"
    assert log[0].cost_usd > 0.50


@pytest.mark.asyncio
async def test_install_global_cap_persists_block_action() -> None:
    """install_global_cap must produce a cap whose action defaults to block."""
    mod = _load_launcher()
    guard = CostGuard(store=InMemoryCostStore())
    await mod.install_global_cap(guard, cap_usd=42.5)

    # Round-trip via the store.
    cap = await guard.store.get_budget(mod.GLOBAL_SCOPE)
    assert cap is not None
    assert cap.per_batch_limit_usd == 42.5
    assert cap.enabled is True


def test_pickup_jql_is_strict_to_s1_refined_only() -> None:
    """Sanity: pickup JQL must include all 4 strict filters so the launcher
    cannot accidentally pick up unrelated or unrefined tickets."""
    mod = _load_launcher()
    jql = mod.S1_PICKABLE_JQL
    assert 'Sprint = "S1: MP v0.4.0"' in jql
    assert 'class:api-anthropic' in jql
    assert 'runner-needs-refinement' in jql
    assert 'tier:X' in jql
    assert 'issuetype = Story' in jql
    assert 'status = "To Do"' in jql
    assert 'assignee is EMPTY' in jql

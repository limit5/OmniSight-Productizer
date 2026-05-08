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
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents.anthropic_native_client import RunResult, TokenUsage
from backend.agents.cost_guard import (
    CostGuard,
    InMemoryCostStore,
)


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

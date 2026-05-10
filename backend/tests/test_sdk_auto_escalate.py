"""OP-816 — SDK launcher auto-escalates one structural Sonnet failure to Opus."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from backend.agents.cost_guard import CostGuard, InMemoryCostStore


_LAUNCHER_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "run_s1_via_anthropic_sdk.py"
)


def _load_launcher() -> Any:
    sys.modules.pop("sdk_launcher_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "sdk_launcher_under_test", _LAUNCHER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sdk_launcher_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class _SequenceClient:
    def __init__(self, mod: Any, stop_reasons: list[str], *, opus_output_tokens: int = 500):
        self._mod = mod
        self._stop_reasons = stop_reasons
        self._opus_output_tokens = opus_output_tokens
        self.calls: list[dict[str, Any]] = []

    async def run_with_tools(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        stop_reason = self._stop_reasons.pop(0)
        output_tokens = (
            self._opus_output_tokens
            if kwargs["model"] == self._mod.DEFAULT_MODEL_OPUS
            else 500
        )
        usage = self._mod.TokenUsage(
            input_tokens=2_000,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        return self._mod.RunResult(
            final_text="done",
            usage=usage,
            stop_reason=stop_reason,
            iterations=kwargs["max_iterations"],
            tool_calls=[],
        )


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch) -> Any:
    mod = _load_launcher()
    monkeypatch.setattr(mod, "has_existing_gerrit_ps", lambda ticket_key: False)
    return mod


async def _run_full(
    mod: Any,
    client: _SequenceClient,
    escalation_counts: dict[str, int] | None = None,
) -> tuple[str, list[Any], dict[str, int]]:
    guard = CostGuard(store=InMemoryCostStore())
    outcomes: list[Any] = []
    counts = escalation_counts if escalation_counts is not None else {}
    status = await mod.process_ticket_full(
        client=client,
        guard=guard,
        ticket_key="OP-816",
        ticket_summary="structural escalation",
        ticket_description="desc",
        model=mod.DEFAULT_MODEL_SONNET,
        per_ticket_cap_usd=5.0,
        max_spend_usd=100.0,
        max_iterations=40,
        log_outcome=outcomes.append,
        dry_run=True,
        escalation_counts=counts,
    )
    return status, outcomes, counts


@pytest.mark.asyncio
async def test_structural_stop_escalates_once_to_opus_with_doubled_iterations(
    launcher: Any,
) -> None:
    client = _SequenceClient(launcher, ["max_iterations_exceeded", "end_turn"])

    status, outcomes, counts = await _run_full(launcher, client)

    assert status == "ok"
    assert counts == {"OP-816": 1}
    assert [call["model"] for call in client.calls] == [
        launcher.DEFAULT_MODEL_SONNET,
        launcher.DEFAULT_MODEL_OPUS,
    ]
    assert [call["max_iterations"] for call in client.calls] == [40, 80]
    assert outcomes[0].status == "ok"
    assert outcomes[0].iterations == 120


@pytest.mark.asyncio
async def test_second_structural_stop_surrenders_without_second_escalation(
    launcher: Any,
) -> None:
    client = _SequenceClient(launcher, ["max_tokens", "max_tokens"])

    status, outcomes, counts = await _run_full(launcher, client)

    assert status == "failed"
    assert counts == {"OP-816": 1}
    assert [call["model"] for call in client.calls] == [
        launcher.DEFAULT_MODEL_SONNET,
        launcher.DEFAULT_MODEL_OPUS,
    ]
    assert outcomes[0].error == "non-retryable stop_reason: max_tokens"


@pytest.mark.asyncio
async def test_existing_escalation_count_blocks_another_opus_retry(
    launcher: Any,
) -> None:
    client = _SequenceClient(launcher, ["max_iterations_exceeded"])

    status, outcomes, counts = await _run_full(
        launcher, client, escalation_counts={"OP-816": 1}
    )

    assert status == "failed"
    assert counts == {"OP-816": 1}
    assert [call["model"] for call in client.calls] == [launcher.DEFAULT_MODEL_SONNET]
    assert outcomes[0].error == "non-retryable stop_reason: max_iterations_exceeded"


@pytest.mark.asyncio
async def test_opus_retry_uses_twenty_dollar_attempt_cap(launcher: Any) -> None:
    client = _SequenceClient(
        launcher,
        ["max_iterations_exceeded", "end_turn"],
        opus_output_tokens=300_000,
    )

    status, outcomes, counts = await _run_full(launcher, client)

    assert status == "ticket_capped"
    assert counts == {"OP-816": 1}
    assert [call["model"] for call in client.calls] == [
        launcher.DEFAULT_MODEL_SONNET,
        launcher.DEFAULT_MODEL_OPUS,
    ]
    assert outcomes[0].error == "per-ticket cap exceeded"
    assert outcomes[0].cost_usd > launcher.DEFAULT_STRUCTURAL_RETRY_CAP_USD

"""MP.W9.4 -- BP.F + BP.C + MP routing integration tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend import graph_topology as gt
from backend.a2a.agent_card import resolve_specialist_a2a_endpoint
from backend.agents import routing_policy
from backend.agents.provider_orchestrator import (
    DispatchResult,
    HealthStatus,
    ProviderAdapter,
    ProviderNotRegistered,
    TaskSpec,
)
from backend.agents.provider_quota_tracker import QuotaState
from backend.t_shirt_sizer import size_project


BASE_URL = "https://omnisight.example.com"


class _FakeAdapter(ProviderAdapter):
    def __init__(
        self,
        provider_id: str,
        *,
        rolling_5h_tokens: int = 0,
        reachable: bool = True,
    ) -> None:
        self._provider_id = provider_id
        self._reachable = reachable
        self._quota_state = QuotaState(
            provider=provider_id,
            rolling_5h_tokens=rolling_5h_tokens,
            weekly_tokens=0,
            last_reset_at=None,
            last_cap_hit_at=None,
            circuit_state="closed",
        )
        self.dispatches: list[TaskSpec] = []

    def provider_id(self) -> str:
        return self._provider_id

    def dispatch(self, task: TaskSpec) -> DispatchResult:
        self.dispatches.append(task)
        return DispatchResult(
            success=True,
            tokens_used=1,
            latency_seconds=0.1,
            error=None,
            provider_id=self._provider_id,
        )

    def health_check(self) -> HealthStatus:
        return HealthStatus(
            provider_id=self._provider_id,
            reachable=self._reachable,
            last_checked_at=datetime.now(timezone.utc),
        )

    def get_quota_state(self) -> QuotaState:
        return self._quota_state


class _FakeOrchestrator:
    def __init__(self, adapters: list[_FakeAdapter]) -> None:
        self._adapters = {adapter.provider_id(): adapter for adapter in adapters}

    def list_adapters(self) -> list[str]:
        return sorted(self._adapters)

    def get_adapter(self, provider_id: str) -> _FakeAdapter:
        try:
            return self._adapters[provider_id]
        except KeyError as exc:
            raise ProviderNotRegistered(provider_id) from exc


@pytest.fixture(autouse=True)
def _restore_recent_caps():
    with routing_policy._RECENTLY_CAPPED_LOCK:
        caps_before = dict(routing_policy._recently_capped)
        routing_policy._recently_capped.clear()

    yield

    with routing_policy._RECENTLY_CAPPED_LOCK:
        routing_policy._recently_capped.clear()
        routing_policy._recently_capped.update(caps_before)


async def _ask_m_size(model: str, prompt: str) -> tuple[str, int]:
    assert model == "test-sizer"
    assert "USER REQUEST" in prompt
    return ('{"size":"M","confidence":0.91,"rationale":"integration tests"}', 31)


@pytest.mark.asyncio
async def test_m_sized_validator_task_uses_bp_f_openai_mapping_for_mp_routing(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_PROVIDER_CAP_OPENAI_SUBSCRIPTION_5H", "100")
    monkeypatch.setenv("OMNISIGHT_PROVIDER_CAP_ANTHROPIC_SUBSCRIPTION_5H", "100")

    report = await size_project(
        "Add integration tests for BP.F + BP.C + MP routing.",
        ask_fn=_ask_m_size,
        models=("test-sizer",),
    )
    topology = gt.build_topology(report.size)
    endpoint = resolve_specialist_a2a_endpoint(
        BASE_URL,
        provider_id="openai",
        agent_name="validator",
    )
    openai = _FakeAdapter("openai-subscription", rolling_5h_tokens=5)
    anthropic = _FakeAdapter("anthropic-subscription", rolling_5h_tokens=1)

    candidates = routing_policy.RoutingPolicy(
        orchestrator=_FakeOrchestrator([anthropic, openai])
    ).choose_provider(
        TaskSpec(
            prompt="Route validator test task through provider-scoped A2A endpoint.",
            agent_class="subscription-codex",
            tier=report.size,
            area=["tests"],
            correlation_id="OP-76",
        )
    )

    assert report.size == "M"
    assert "validator" in topology.get_graph().nodes
    assert endpoint.model_spec == "openai:gpt-4o"
    assert endpoint.endpoint_url == f"{BASE_URL}/a2a/providers/openai/invoke/validator"
    assert [adapter.provider_id() for adapter in candidates] == ["openai-subscription"]


@pytest.mark.asyncio
async def test_bp_f_anthropic_reviewer_mapping_routes_matching_mp_provider(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OMNISIGHT_PROVIDER_CAP_OPENAI_SUBSCRIPTION_5H", "100")
    monkeypatch.setenv("OMNISIGHT_PROVIDER_CAP_ANTHROPIC_SUBSCRIPTION_5H", "100")

    report = await size_project(
        "Review the standard DAG integration test plan.",
        ask_fn=_ask_m_size,
        models=("test-sizer",),
    )
    topology = gt.build_topology(report.size)
    endpoint = resolve_specialist_a2a_endpoint(
        BASE_URL,
        provider_id="anthropic",
        agent_name="reviewer",
    )
    openai = _FakeAdapter("openai-subscription", rolling_5h_tokens=1)
    anthropic = _FakeAdapter("anthropic-subscription", rolling_5h_tokens=5)

    candidates = routing_policy.RoutingPolicy(
        orchestrator=_FakeOrchestrator([openai, anthropic])
    ).choose_provider(
        TaskSpec(
            prompt="Route reviewer task through provider-scoped A2A endpoint.",
            agent_class="subscription-claude",
            tier=report.size,
            area=["tests"],
            correlation_id="OP-76",
        )
    )

    assert report.size == "M"
    assert "reviewer" in topology.get_graph().nodes
    assert endpoint.model_spec == "anthropic:claude-sonnet-4-20250514"
    assert endpoint.endpoint_url == f"{BASE_URL}/a2a/providers/anthropic/invoke/reviewer"
    assert [adapter.provider_id() for adapter in candidates] == ["anthropic-subscription"]

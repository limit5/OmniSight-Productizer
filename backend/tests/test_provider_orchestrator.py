"""MP.W1.7 -- provider orchestrator registry, routing, and circuit tests."""

from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from backend.agents import provider_orchestrator as orchestrator
from backend.agents import routing_policy
from backend.agents.provider_orchestrator import (
    CircuitBreaker,
    DispatchResult,
    HealthStatus,
    ProviderAdapter,
    ProviderNotRegistered,
    TaskSpec,
)
from backend.agents.provider_quota_tracker import QuotaState


class _FakeAdapter(ProviderAdapter):
    def __init__(
        self,
        provider_id: str,
        *,
        reachable: bool = True,
        quota_state: QuotaState | None = None,
        dispatch_success: bool = True,
        health_raises: bool = False,
        quota_raises: bool = False,
    ) -> None:
        self._provider_id = provider_id
        self._reachable = reachable
        self._quota_state = quota_state or _quota_state(provider_id)
        self._dispatch_success = dispatch_success
        self._health_raises = health_raises
        self._quota_raises = quota_raises
        self.dispatches: list[TaskSpec] = []

    def provider_id(self) -> str:
        return self._provider_id

    def dispatch(self, task: TaskSpec) -> DispatchResult:
        self.dispatches.append(task)
        return DispatchResult(
            success=self._dispatch_success,
            tokens_used=17,
            latency_seconds=0.25,
            error=None if self._dispatch_success else "fake failure",
            provider_id=self._provider_id,
        )

    def health_check(self) -> HealthStatus:
        if self._health_raises:
            raise RuntimeError("health failed")
        return HealthStatus(
            provider_id=self._provider_id,
            reachable=self._reachable,
            last_checked_at=datetime.now(timezone.utc),
        )

    def get_quota_state(self) -> QuotaState:
        if self._quota_raises:
            raise RuntimeError("quota failed")
        return self._quota_state


class _FakeOrchestrator:
    def __init__(self, adapters: list[_FakeAdapter], missing: set[str] | None = None):
        self._adapters = {adapter.provider_id(): adapter for adapter in adapters}
        self._missing = missing or set()

    def list_adapters(self) -> list[str]:
        return sorted(set(self._adapters) | self._missing)

    def get_adapter(self, provider_id: str) -> _FakeAdapter:
        if provider_id in self._missing:
            raise ProviderNotRegistered(provider_id)
        return self._adapters[provider_id]


@pytest.fixture(autouse=True)
def _restore_registry_and_caps():
    with orchestrator._REGISTRY_LOCK:
        registry_before = dict(orchestrator._REGISTRY)
        orchestrator._REGISTRY.clear()
    with routing_policy._RECENTLY_CAPPED_LOCK:
        caps_before = dict(routing_policy._recently_capped)
        routing_policy._recently_capped.clear()

    yield

    with orchestrator._REGISTRY_LOCK:
        orchestrator._REGISTRY.clear()
        orchestrator._REGISTRY.update(registry_before)
    with routing_policy._RECENTLY_CAPPED_LOCK:
        routing_policy._recently_capped.clear()
        routing_policy._recently_capped.update(caps_before)


def _quota_state(
    provider: str,
    *,
    rolling_5h_tokens: int = 0,
    weekly_tokens: int = 0,
    circuit_state: str = "closed",
) -> QuotaState:
    return QuotaState(
        provider=provider,
        rolling_5h_tokens=rolling_5h_tokens,
        weekly_tokens=weekly_tokens,
        last_reset_at=None,
        last_cap_hit_at=None,
        circuit_state=circuit_state,
    )


def _task(
    *,
    agent_class: str = "api-anthropic",
    tier: str = "M",
) -> TaskSpec:
    return TaskSpec(
        prompt="run OP-20",
        agent_class=agent_class,
        tier=tier,
        area=["backend", "tests"],
        correlation_id="op-20",
    )


def _policy(
    adapters: list[_FakeAdapter],
    *,
    now=lambda: 1000.0,
    missing: set[str] | None = None,
    human_assignment_resolver=None,
) -> routing_policy.RoutingPolicy:
    return routing_policy.RoutingPolicy(
        orchestrator=_FakeOrchestrator(adapters, missing),
        now=now,
        human_assignment_resolver=human_assignment_resolver,
    )


# Registry contract


def test_register_adapter_get_adapter_round_trip() -> None:
    adapter = _FakeAdapter("op20-a")

    orchestrator.register_adapter(adapter)

    assert orchestrator.get_adapter("op20-a") is adapter


def test_register_adapter_normalises_provider_id() -> None:
    adapter = _FakeAdapter("  op20-trimmed  ")

    orchestrator.register_adapter(adapter)

    assert orchestrator.get_adapter("op20-trimmed") is adapter


def test_get_adapter_normalises_lookup_id() -> None:
    adapter = _FakeAdapter("op20-lookup")
    orchestrator.register_adapter(adapter)

    assert orchestrator.get_adapter("  op20-lookup  ") is adapter


def test_list_adapters_returns_sorted_ids() -> None:
    orchestrator.register_adapter(_FakeAdapter("op20-z"))
    orchestrator.register_adapter(_FakeAdapter("op20-a"))
    orchestrator.register_adapter(_FakeAdapter("op20-m"))

    assert orchestrator.list_adapters() == ["op20-a", "op20-m", "op20-z"]


def test_register_adapter_replaces_existing_provider() -> None:
    first = _FakeAdapter("op20-replace")
    second = _FakeAdapter("op20-replace")

    orchestrator.register_adapter(first)
    orchestrator.register_adapter(second)

    assert orchestrator.get_adapter("op20-replace") is second


def test_get_adapter_missing_provider_raises_registered_error() -> None:
    with pytest.raises(ProviderNotRegistered) as excinfo:
        orchestrator.get_adapter("op20-missing")

    assert excinfo.value.args == ("op20-missing",)


def test_register_adapter_rejects_empty_provider_id() -> None:
    with pytest.raises(ValueError, match="provider_id must be non-empty"):
        orchestrator.register_adapter(_FakeAdapter("  "))


def test_get_adapter_rejects_empty_provider_id() -> None:
    with pytest.raises(ValueError, match="provider_id must be non-empty"):
        orchestrator.get_adapter("\t")


def test_task_spec_is_immutable() -> None:
    task = _task()

    with pytest.raises(FrozenInstanceError):
        task.prompt = "changed"  # type: ignore[misc]


def test_dispatch_result_is_immutable() -> None:
    result = DispatchResult(
        success=True,
        tokens_used=1,
        latency_seconds=0.1,
        error=None,
        provider_id="op20-result",
    )

    with pytest.raises(FrozenInstanceError):
        result.success = False  # type: ignore[misc]


def test_concurrent_registry_writes_are_visible_after_join() -> None:
    adapters = [_FakeAdapter(f"op20-thread-{idx}") for idx in range(12)]
    threads = [
        threading.Thread(target=orchestrator.register_adapter, args=(adapter,))
        for adapter in adapters
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(orchestrator.get_adapter(adapter.provider_id()) is adapter for adapter in adapters)


# Circuit breaker contract


def test_circuit_breaker_starts_closed_when_quota_state_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_get_quota_state",
        lambda provider: _quota_state(provider, circuit_state="closed"),
    )

    assert CircuitBreaker("op20-breaker").is_open() is False


def test_circuit_breaker_honours_persisted_open_state(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_get_quota_state",
        lambda provider: _quota_state(provider, circuit_state="open"),
    )

    assert CircuitBreaker("op20-breaker").is_open() is True


def test_circuit_breaker_rejects_after_threshold_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_get_quota_state",
        lambda provider: _quota_state(provider),
    )
    breaker = CircuitBreaker("op20-breaker")

    for _ in range(CircuitBreaker.trip_threshold - 1):
        breaker.record_outcome(False)

    assert breaker.is_open() is False
    breaker.record_outcome(False)
    assert breaker.is_open() is True


def test_circuit_breaker_success_resets_failure_count(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_get_quota_state",
        lambda provider: _quota_state(provider),
    )
    breaker = CircuitBreaker("op20-breaker")

    for _ in range(CircuitBreaker.trip_threshold - 1):
        breaker.record_outcome(False)
    breaker.record_outcome(True)
    breaker.record_outcome(False)

    assert breaker.is_open() is False


def test_circuit_breaker_success_closes_open_local_state(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator,
        "_get_quota_state",
        lambda provider: _quota_state(provider),
    )
    breaker = CircuitBreaker("op20-breaker")

    for _ in range(CircuitBreaker.trip_threshold):
        breaker.record_outcome(False)
    breaker.record_outcome(True)

    assert breaker.is_open() is False


def test_circuit_breaker_remains_open_before_cooldown(monkeypatch) -> None:
    now = {"value": 10.0}
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        orchestrator,
        "_get_quota_state",
        lambda provider: _quota_state(provider),
    )
    breaker = CircuitBreaker("op20-breaker")

    for _ in range(CircuitBreaker.trip_threshold):
        breaker.record_outcome(False)
    now["value"] += CircuitBreaker.cooldown_seconds - 1

    assert breaker.is_open() is True


def test_circuit_breaker_closes_after_cooldown(monkeypatch) -> None:
    now = {"value": 10.0}
    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        orchestrator,
        "_get_quota_state",
        lambda provider: _quota_state(provider),
    )
    breaker = CircuitBreaker("op20-breaker")

    for _ in range(CircuitBreaker.trip_threshold):
        breaker.record_outcome(False)
    now["value"] += CircuitBreaker.cooldown_seconds

    assert breaker.is_open() is False
    for _ in range(CircuitBreaker.trip_threshold - 1):
        breaker.record_outcome(False)
    assert breaker.is_open() is False


def test_circuit_breaker_normalises_provider_id_before_quota_lookup(monkeypatch) -> None:
    seen: list[str] = []

    def _fake_quota_state(provider: str) -> QuotaState:
        seen.append(provider)
        return _quota_state(provider)

    monkeypatch.setattr(orchestrator, "_get_quota_state", _fake_quota_state)

    assert CircuitBreaker("  op20-normalised  ").is_open() is False
    assert seen == ["op20-normalised"]


def test_circuit_breaker_rejects_empty_provider_id() -> None:
    with pytest.raises(ValueError, match="provider_id must be non-empty"):
        CircuitBreaker(" ")


# Routing policy contract


def test_routing_choose_provider_returns_allowed_healthy_provider() -> None:
    adapter = _FakeAdapter("anthropic-subscription")

    chosen = _policy([adapter]).choose_provider(_task(agent_class="api-anthropic"))

    assert chosen == [adapter]


def test_routing_excludes_unhealthy_provider() -> None:
    healthy = _FakeAdapter("anthropic-subscription")
    unhealthy = _FakeAdapter("openai-subscription", reachable=False)

    chosen = _policy([healthy, unhealthy]).choose_provider(_task(agent_class="api-openai"))

    assert chosen == []


def test_routing_excludes_provider_when_health_check_raises() -> None:
    adapter = _FakeAdapter("anthropic-subscription", health_raises=True)

    chosen = _policy([adapter]).choose_provider(_task(agent_class="api-anthropic"))

    assert chosen == []


def test_routing_excludes_provider_when_quota_state_raises() -> None:
    adapter = _FakeAdapter("anthropic-subscription", quota_raises=True)

    chosen = _policy([adapter]).choose_provider(_task(agent_class="api-anthropic"))

    assert chosen == []


def test_routing_excludes_open_circuit_quota_state() -> None:
    adapter = _FakeAdapter(
        "anthropic-subscription",
        quota_state=_quota_state("anthropic-subscription", circuit_state="open"),
    )

    chosen = _policy([adapter]).choose_provider(_task(agent_class="api-anthropic"))

    assert chosen == []


def test_routing_skips_missing_registry_entry() -> None:
    chosen = _policy([], missing={"anthropic-subscription"}).choose_provider(
        _task(agent_class="api-anthropic")
    )

    assert chosen == []


def test_routing_filters_by_agent_class_provider_family() -> None:
    anthropic = _FakeAdapter("anthropic-subscription")
    openai = _FakeAdapter("openai-subscription")

    chosen = _policy([anthropic, openai]).choose_provider(
        _task(agent_class="api-openai")
    )

    assert chosen == [openai]


def test_routing_orders_by_remaining_quota_then_provider_id(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_PROVIDER_CAP_OPENAI_SUBSCRIPTION_5H", "100")
    monkeypatch.setenv("OMNISIGHT_PROVIDER_CAP_OPENAI_API_5H", "100")
    high = _FakeAdapter(
        "openai-subscription",
        quota_state=_quota_state("openai-subscription", rolling_5h_tokens=10),
    )
    low = _FakeAdapter(
        "openai-api",
        quota_state=_quota_state("openai-api", rolling_5h_tokens=60),
    )

    chosen = _policy([low, high]).choose_provider(_task(agent_class="api-openai"))

    assert chosen == [high, low]


def test_routing_tier_l_prefers_high_quota_provider_when_available(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_PROVIDER_CAP_OPENAI_SUBSCRIPTION_5H", "100")
    monkeypatch.setenv("OMNISIGHT_PROVIDER_CAP_OPENAI_API_5H", "100")
    low = _FakeAdapter(
        "openai-subscription",
        quota_state=_quota_state("openai-subscription", rolling_5h_tokens=75),
    )
    high = _FakeAdapter(
        "openai-api",
        quota_state=_quota_state("openai-api", rolling_5h_tokens=25),
    )

    chosen = _policy([low, high]).choose_provider(
        _task(agent_class="api-openai", tier="L")
    )

    assert chosen == [high]


def test_routing_tier_l_keeps_low_quota_candidates_when_no_high_quota(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_PROVIDER_CAP_OPENAI_SUBSCRIPTION_5H", "100")
    low = _FakeAdapter(
        "openai-subscription",
        quota_state=_quota_state("openai-subscription", rolling_5h_tokens=75),
    )

    chosen = _policy([low]).choose_provider(_task(agent_class="api-openai", tier="L"))

    assert chosen == [low]


def test_routing_tier_x_requires_human_assignment() -> None:
    adapter = _FakeAdapter("anthropic-subscription")

    chosen = _policy([adapter]).choose_provider(
        _task(agent_class="api-anthropic", tier="X")
    )

    assert chosen == []


def test_routing_tier_x_allows_human_assigned_provider() -> None:
    adapter = _FakeAdapter("anthropic-subscription")

    chosen = _policy(
        [adapter],
        human_assignment_resolver=lambda task: "anthropic-subscription",
    ).choose_provider(_task(agent_class="api-anthropic", tier="X"))

    assert chosen == [adapter]


def test_routing_human_assignment_filters_to_requested_provider() -> None:
    anthropic = _FakeAdapter("anthropic-subscription")
    openai = _FakeAdapter("openai-subscription")

    chosen = _policy(
        [anthropic, openai],
        human_assignment_resolver=lambda task: "openai-subscription",
    ).choose_provider(_task(agent_class="api-openai"))

    assert chosen == [openai]


def test_routing_on_cap_hit_suppresses_provider_until_retry_after() -> None:
    now = {"value": 1000.0}
    adapter = _FakeAdapter("anthropic-subscription")
    policy = _policy([adapter], now=lambda: now["value"])

    policy.on_cap_hit("anthropic-subscription", retry_after_s=30)
    assert policy.choose_provider(_task(agent_class="api-anthropic")) == []

    now["value"] = 1031.0
    assert policy.choose_provider(_task(agent_class="api-anthropic")) == [adapter]


def test_routing_on_cap_hit_uses_default_suppression_window() -> None:
    now = {"value": 1000.0}
    adapter = _FakeAdapter("anthropic-subscription")
    policy = _policy([adapter], now=lambda: now["value"])

    policy.on_cap_hit("anthropic-subscription")
    now["value"] += routing_policy.DEFAULT_CAP_SUPPRESSION_S - 1

    assert policy.choose_provider(_task(agent_class="api-anthropic")) == []


def test_routing_on_cap_hit_zero_retry_after_expires_after_now_boundary() -> None:
    now = {"value": 1000.0}
    adapter = _FakeAdapter("anthropic-subscription")
    policy = _policy([adapter], now=lambda: now["value"])

    policy.on_cap_hit("anthropic-subscription", retry_after_s=0)
    assert policy.choose_provider(_task(agent_class="api-anthropic")) == []
    now["value"] = 1000.001

    assert policy.choose_provider(_task(agent_class="api-anthropic")) == [adapter]


def test_routing_invalid_cap_hit_provider_id_raises() -> None:
    with pytest.raises(ValueError, match="provider_id must be non-empty"):
        _policy([]).on_cap_hit(" ")

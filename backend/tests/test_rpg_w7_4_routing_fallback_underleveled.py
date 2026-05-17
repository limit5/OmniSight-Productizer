"""RPG.W7.4 — routing fallback when the preferred instance is under-leveled.

ADR-0008 §"Routing integration" promises that when a Tier X (or BP.C
size-gated) task names a preferred provider via ``prefer_agent_id`` and
that provider fails the tier gate, the router must:

* skip the under-leveled preferred provider,
* fall back to the remaining eligible candidates (best-first sort), and
* emit one structured WARNING that carries the preferred provider id
  and the ``unmet_reasons`` tuple from
  :class:`backend.agents.tier_gate.TierGateDecision` so operators can
  see *why* the preferred instance was skipped.

W7.1 landed the call site inside ``RoutingPolicy.choose_provider`` and
W7.2/W7.3 own the pure gate semantics. This module pins the *fallback
behaviour* end-to-end: the test matrix is
{preferred eligible, preferred under-leveled} × {fallbacks eligible,
no fallbacks eligible}.

These tests intentionally inject a stub ``tier_gate_decision_resolver``
rather than wiring real character-card + skill-state stores — the gate
itself is unit-tested in ``test_tier_gate``; here we only care that the
router consumes the decision exactly as documented.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.agents import routing_policy
from backend.agents.provider_orchestrator import (
    DispatchResult,
    HealthStatus,
    ProviderAdapter,
    ProviderNotRegistered,
    TaskSpec,
)
from backend.agents.provider_quota_tracker import QuotaState
from backend.agents.tier_gate import TierGateDecision


class _FakeAdapter(ProviderAdapter):
    def __init__(self, provider_id: str) -> None:
        self._provider_id = provider_id
        self._quota_state = QuotaState(
            provider=provider_id,
            rolling_5h_tokens=0,
            weekly_tokens=0,
            last_reset_at=None,
            last_cap_hit_at=None,
            circuit_state="closed",
        )

    def provider_id(self) -> str:
        return self._provider_id

    def dispatch(self, task: TaskSpec) -> DispatchResult:
        return DispatchResult(
            success=True,
            tokens_used=1,
            latency_seconds=0.0,
            error=None,
            provider_id=self._provider_id,
        )

    def health_check(self) -> HealthStatus:
        return HealthStatus(
            provider_id=self._provider_id,
            reachable=True,
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


def _tier_x_task(prefer_agent_id: str | None = None) -> TaskSpec:
    return TaskSpec(
        prompt="route a Tier X task to a level-gated provider",
        agent_class="api-anthropic",
        tier="X",
        area=["backend", "tests"],
        correlation_id="OP-1363",
        prefer_agent_id=prefer_agent_id,
    )


def _gate_eligible(provider_id: str, *, tier: str = "X") -> TierGateDecision:
    return TierGateDecision(
        eligible=True,
        tier=tier,
        agent_level=80,
        skill_level=5,
        skill_id="backend",
        unmet_reasons=(),
    )


def _gate_under_leveled(
    provider_id: str,
    *,
    tier: str = "X",
    agent_level: int = 42,
    skill_level: int = 2,
) -> TierGateDecision:
    reasons: tuple[str, ...] = ()
    if agent_level < 50:
        reasons += (f"tier_x_agent_level_below_50:{agent_level}",)
    if skill_level < 3:
        reasons += (f"tier_x_skill_level_below_3:{skill_level}",)
    return TierGateDecision(
        eligible=False,
        tier=tier,
        agent_level=agent_level,
        skill_level=skill_level,
        skill_id="backend",
        unmet_reasons=reasons,
    )


def _make_policy(
    adapters: list[_FakeAdapter],
    decisions: dict[str, TierGateDecision],
) -> routing_policy.RoutingPolicy:
    def _resolver(_task: TaskSpec, provider_id: str) -> TierGateDecision:
        return decisions[provider_id]

    return routing_policy.RoutingPolicy(
        orchestrator=_FakeOrchestrator(adapters),
        tier_gate_decision_resolver=_resolver,
    )


# ── AC1 ────────────────────────────────────────────────────────────


def test_underleveled_preferred_falls_back_to_eligible_with_warning(caplog) -> None:
    """Preferred under-leveled → router skips it, falls back, warns once.

    Pins the W7.1 call site behaviour: the WARNING includes the
    preferred provider id AND the structured ``unmet_reasons`` tuple
    from the tier-gate decision so operators can root-cause without
    re-deriving the gate.
    """

    preferred = _FakeAdapter("anthropic-beta")
    fallback = _FakeAdapter("anthropic-alpha")
    policy = _make_policy(
        [fallback, preferred],
        {
            "anthropic-beta": _gate_under_leveled("anthropic-beta"),
            "anthropic-alpha": _gate_eligible("anthropic-alpha"),
        },
    )

    with caplog.at_level("WARNING", logger="backend.agents.routing_policy"):
        chosen = policy.choose_provider(_tier_x_task(prefer_agent_id="anthropic-beta"))

    assert [adapter.provider_id() for adapter in chosen] == ["anthropic-alpha"]

    matching = [
        record
        for record in caplog.records
        if "routing preferred provider anthropic-beta under-leveled" in record.message
    ]
    assert len(matching) == 1, (
        "expected exactly one under-leveled fallback warning; "
        f"got {[record.message for record in caplog.records]!r}"
    )
    assert "tier_x_agent_level_below_50:42" in matching[0].message
    assert "tier_x_skill_level_below_3:2" in matching[0].message


# ── AC2 ────────────────────────────────────────────────────────────


def test_underleveled_preferred_returns_all_remaining_eligible_candidates() -> None:
    """Fallback set is *all* other eligible candidates, not just one.

    The router excludes only the under-leveled preferred provider; the
    remaining eligible candidates are returned best-first per the
    tier-aware sort. This guards against a regression where the
    fallback path accidentally collapsed to a single provider.
    """

    preferred = _FakeAdapter("anthropic-beta")
    alpha = _FakeAdapter("anthropic-alpha")
    gamma = _FakeAdapter("anthropic-gamma")
    policy = _make_policy(
        [alpha, gamma, preferred],
        {
            "anthropic-beta": _gate_under_leveled("anthropic-beta"),
            "anthropic-alpha": _gate_eligible("anthropic-alpha"),
            "anthropic-gamma": _gate_eligible("anthropic-gamma"),
        },
    )

    chosen = policy.choose_provider(_tier_x_task(prefer_agent_id="anthropic-beta"))

    ids = {adapter.provider_id() for adapter in chosen}
    assert ids == {"anthropic-alpha", "anthropic-gamma"}
    assert "anthropic-beta" not in ids


# ── AC3 ────────────────────────────────────────────────────────────


def test_underleveled_preferred_with_no_eligible_fallback_returns_empty(caplog) -> None:
    """Preferred under-leveled AND every fallback also under-leveled → [].

    The warning still fires (operators need to see the preferred miss
    even when there is nothing to fall back to), but the router refuses
    to dispatch — under-leveling is never silently downgraded to a
    non-preferred ineligible candidate.
    """

    preferred = _FakeAdapter("anthropic-beta")
    fallback = _FakeAdapter("anthropic-alpha")
    policy = _make_policy(
        [fallback, preferred],
        {
            "anthropic-beta": _gate_under_leveled("anthropic-beta"),
            "anthropic-alpha": _gate_under_leveled(
                "anthropic-alpha", agent_level=10, skill_level=0
            ),
        },
    )

    with caplog.at_level("WARNING", logger="backend.agents.routing_policy"):
        chosen = policy.choose_provider(_tier_x_task(prefer_agent_id="anthropic-beta"))

    assert chosen == []
    assert any(
        "routing preferred provider anthropic-beta under-leveled" in record.message
        for record in caplog.records
    ), "operator-visible warning must still fire when no fallback is eligible"


# ── AC4 ────────────────────────────────────────────────────────────


def test_eligible_preferred_returned_without_fallback_warning(caplog) -> None:
    """Preferred eligible → only preferred is returned, no warning fires.

    This is the control case for AC1/AC2/AC3: the under-leveled-fallback
    branch must be quiet when the preferred provider passes the gate,
    otherwise telemetry would log false positives on every healthy
    Tier X dispatch.
    """

    preferred = _FakeAdapter("anthropic-beta")
    fallback = _FakeAdapter("anthropic-alpha")
    policy = _make_policy(
        [fallback, preferred],
        {
            "anthropic-beta": _gate_eligible("anthropic-beta"),
            "anthropic-alpha": _gate_eligible("anthropic-alpha"),
        },
    )

    with caplog.at_level("WARNING", logger="backend.agents.routing_policy"):
        chosen = policy.choose_provider(_tier_x_task(prefer_agent_id="anthropic-beta"))

    assert [adapter.provider_id() for adapter in chosen] == ["anthropic-beta"]
    assert not any(
        "under-leveled" in record.message for record in caplog.records
    ), "no under-leveled fallback warning should fire when preferred is eligible"

"""MP.W1.6 -- cap-aware provider routing policy.

This module chooses the registered ``ProviderAdapter`` candidates for a
``TaskSpec`` at task-dispatch boundaries.  It intentionally does not switch
providers mid-task; callers should invoke ``choose_provider()`` before the next
task or retry boundary after recording a cap hit with ``on_cap_hit()``.

Module-global state audit (per project SOP)
-------------------------------------------
``_recently_capped`` is a module-level ``dict[str, float]`` mapping provider id
to a monotonic expiry timestamp.  It is guarded by ``_RECENTLY_CAPPED_LOCK``, a
module-level ``threading.RLock``.  Mutation is only exposed through
``on_cap_hit()`` / ``RoutingPolicy.on_cap_hit()``; routing reads and prunes the
dict under the same lock before filtering providers.

Import side-effect contract
---------------------------
Importing this module imports the MVP subscription adapters so their existing
registration side effects populate ``provider_orchestrator``.  Future provider
adapters should follow the same register-on-import pattern before they are
eligible for routing.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock

from backend.agents import provider_orchestrator
from backend.agents.provider_orchestrator import ProviderAdapter, TaskSpec
from backend.agents.provider_quota_tracker import DEFAULT_5H_CAP_TOKENS, QuotaState

# Register shipped MVP providers.
import backend.agents.provider_adapters.anthropic_subscription  # noqa: F401,E402
import backend.agents.provider_adapters.openai_subscription  # noqa: F401,E402


DEFAULT_CAP_SUPPRESSION_S = 5 * 60 * 60
HIGH_QUOTA_RATIO = 0.50

_recently_capped: dict[str, float] = {}
_RECENTLY_CAPPED_LOCK = RLock()

HumanAssignmentResolver = Callable[[TaskSpec], str | None]


@dataclass(frozen=True)
class _Candidate:
    adapter: ProviderAdapter
    provider_id: str
    quota_state: QuotaState
    remaining_5h_quota_ratio: float
    circuit_open_count: int


class RoutingPolicy:
    """Choose provider adapters for one task at task-boundary time."""

    def __init__(
        self,
        *,
        orchestrator: object = provider_orchestrator,
        now: Callable[[], float] = time.monotonic,
        human_assignment_resolver: HumanAssignmentResolver | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._now = now
        self._human_assignment_resolver = (
            human_assignment_resolver or _default_human_assignment_resolver
        )

    def choose_provider(self, task: TaskSpec) -> list[ProviderAdapter]:
        """Return ranked acceptable providers for ``task``.

        The result is best-first.  An empty list means no currently acceptable
        provider is registered, healthy, allowed by ``agent_class``, and eligible
        for the task tier.
        """
        assigned_provider_id = self._human_assignment_resolver(task)
        if _normalise_tier(task.tier) == "X" and assigned_provider_id is None:
            return []

        candidates = self._healthy_candidates(task)
        if assigned_provider_id is not None:
            candidates = [
                candidate
                for candidate in candidates
                if candidate.provider_id == assigned_provider_id
            ]

        if _normalise_tier(task.tier) == "L":
            high_quota = [
                candidate
                for candidate in candidates
                if candidate.remaining_5h_quota_ratio > HIGH_QUOTA_RATIO
            ]
            if high_quota:
                candidates = high_quota

        candidates.sort(
            key=lambda candidate: (
                -candidate.remaining_5h_quota_ratio,
                candidate.circuit_open_count,
                candidate.provider_id,
            )
        )
        return [candidate.adapter for candidate in candidates]

    def on_cap_hit(self, provider_id: str, retry_after_s: int | None = None) -> None:
        """Record a task-boundary cap hit and suppress routing to the provider."""
        provider_id = _normalise_provider_id(provider_id)
        wait_s = DEFAULT_CAP_SUPPRESSION_S if retry_after_s is None else retry_after_s
        until_ts = self._now() + max(wait_s, 0)
        with _RECENTLY_CAPPED_LOCK:
            _recently_capped[provider_id] = until_ts

    def _healthy_candidates(self, task: TaskSpec) -> list[_Candidate]:
        self._prune_recently_capped()
        candidates: list[_Candidate] = []
        for provider_id in self._list_provider_ids():
            provider_id = _normalise_provider_id(provider_id)
            if self._is_recently_capped(provider_id):
                continue

            adapter = self._get_adapter(provider_id)
            if adapter is None:
                continue
            quota_state = _quota_state(adapter)
            if quota_state is None or quota_state.circuit_state == "open":
                continue
            health = _health_status(adapter)
            if health is None or not health.reachable:
                continue
            if not _agent_class_allows_provider(task.agent_class, provider_id):
                continue

            candidates.append(
                _Candidate(
                    adapter=adapter,
                    provider_id=provider_id,
                    quota_state=quota_state,
                    remaining_5h_quota_ratio=_remaining_5h_quota_ratio(quota_state),
                    circuit_open_count=_circuit_open_count(quota_state),
                )
            )
        return candidates

    def _list_provider_ids(self) -> list[str]:
        return list(self._orchestrator.list_adapters())  # type: ignore[attr-defined]

    def _get_adapter(self, provider_id: str) -> ProviderAdapter | None:
        try:
            return self._orchestrator.get_adapter(provider_id)  # type: ignore[attr-defined]
        except provider_orchestrator.ProviderNotRegistered:
            return None

    def _is_recently_capped(self, provider_id: str) -> bool:
        now = self._now()
        with _RECENTLY_CAPPED_LOCK:
            until_ts = _recently_capped.get(provider_id)
        return until_ts is not None and now <= until_ts

    def _prune_recently_capped(self) -> None:
        now = self._now()
        with _RECENTLY_CAPPED_LOCK:
            expired = [
                provider_id
                for provider_id, until_ts in _recently_capped.items()
                if now > until_ts
            ]
            for provider_id in expired:
                del _recently_capped[provider_id]


def _quota_state(adapter: ProviderAdapter) -> QuotaState | None:
    try:
        return adapter.get_quota_state()
    except Exception:
        return None


def _health_status(adapter: ProviderAdapter):
    try:
        return adapter.health_check()
    except Exception:
        return None


def _remaining_5h_quota_ratio(state: QuotaState) -> float:
    cap = _provider_5h_cap(state.provider)
    remaining = max(cap - state.rolling_5h_tokens, 0)
    return remaining / cap


def _provider_5h_cap(provider_id: str) -> int:
    env_name = f"OMNISIGHT_PROVIDER_CAP_{_env_provider(provider_id)}_5H"
    raw = (os.environ.get(env_name) or "").strip()
    if raw:
        try:
            cap = int(raw)
        except ValueError:
            cap = DEFAULT_5H_CAP_TOKENS
        if cap > 0:
            return cap
    return DEFAULT_5H_CAP_TOKENS


def _circuit_open_count(state: QuotaState) -> int:
    return 1 if state.circuit_state == "open" else 0


def _agent_class_allows_provider(agent_class: str, provider_id: str) -> bool:
    agent_class = agent_class.strip()
    provider_id = _normalise_provider_id(provider_id)
    if provider_id == "anthropic-subscription":
        return agent_class in {"subscription-claude", "api-anthropic"}
    if provider_id == "openai-subscription":
        return agent_class in {"subscription-codex", "api-openai"}
    provider_prefix = provider_id.split("-", 1)[0]
    return provider_prefix in agent_class


def _default_human_assignment_resolver(task: TaskSpec) -> str | None:
    for attr in (
        "prefer_agent_id",
        "human_assigned_provider_id",
        "assigned_provider_id",
        "provider_id",
    ):
        value = getattr(task, attr, None)
        if isinstance(value, str) and value.strip():
            return _normalise_provider_id(value)
    return None


def _normalise_provider_id(provider_id: str) -> str:
    out = provider_id.strip()
    if not out:
        raise ValueError("provider_id must be non-empty")
    return out


def _normalise_tier(tier: str) -> str:
    return tier.strip().upper()


def _env_provider(provider_id: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in provider_id.upper())


_DEFAULT_POLICY = RoutingPolicy()


def choose_provider(task: TaskSpec) -> list[ProviderAdapter]:
    """Return ranked provider candidates using the module-default policy."""
    return _DEFAULT_POLICY.choose_provider(task)


def on_cap_hit(provider_id: str, retry_after_s: int | None = None) -> None:
    """Record a cap hit using the module-default policy."""
    _DEFAULT_POLICY.on_cap_hit(provider_id, retry_after_s)


__all__ = [
    "RoutingPolicy",
    "_recently_capped",
    "choose_provider",
    "on_cap_hit",
]

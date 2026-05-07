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
from pathlib import Path
from threading import RLock
from typing import Any

import yaml

from backend import feature_flags
from backend.agents import cost_estimator
from backend.agents import provider_orchestrator
from backend.agents.provider_orchestrator import ProviderAdapter, TaskSpec
from backend.agents.provider_quota_tracker import DEFAULT_5H_CAP_TOKENS, QuotaState
from backend.sandbox_tier import Guild

# Register shipped MVP providers.
import backend.agents.provider_adapters.anthropic_subscription  # noqa: F401,E402
import backend.agents.provider_adapters.gemini_subscription  # noqa: F401,E402
import backend.agents.provider_adapters.openai_subscription  # noqa: F401,E402
import backend.agents.provider_adapters.xai_subscription  # noqa: F401,E402


DEFAULT_CAP_SUPPRESSION_S = 5 * 60 * 60
HIGH_QUOTA_RATIO = 0.50
MP_ENABLED_ENV = "OMNISIGHT_MP_ENABLED"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MODEL_MAPPING_PATH = _PROJECT_ROOT / "configs" / "model_mapping.yaml"

_recently_capped: dict[str, float] = {}
_RECENTLY_CAPPED_LOCK = RLock()
_MODEL_ROUTING_CACHE: tuple[float | None, dict[str, str], set[str]] | None = None

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
        if not is_enabled():
            return []

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

        # OP-75 + post-rebase note (2026-05-07): #92's tier-aware sort
        # supersedes the family-match boost that landed on develop after
        # this change was originally pushed. Tier S explicitly wants
        # predicted-cost as the primary discriminator (per OP-75 commit
        # message), and Tier X is quota-first; Tier M/L fall through to
        # _quota_first_sort_key. The family-match boost from
        # _preferred_provider_family_for_task is intentionally dropped
        # here for Tier S — re-evaluate folding it back into Tier M/L
        # _quota_first_sort_key if that boost was load-bearing for any
        # production routing scenario.
        tier = _normalise_tier(task.tier)
        candidates.sort(key=lambda candidate: _tier_sort_key(tier, task, candidate))
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


def _tier_sort_key(tier: str, task: TaskSpec, candidate: _Candidate) -> tuple:
    if tier == "S":
        return (
            _predicted_cost_usd(task, candidate.adapter),
            -candidate.remaining_5h_quota_ratio,
            candidate.circuit_open_count,
            candidate.provider_id,
        )
    if tier == "X":
        return (
            -candidate.remaining_5h_quota_ratio,
            candidate.circuit_open_count,
            candidate.provider_id,
        )
    return _quota_first_sort_key(candidate)


def _quota_first_sort_key(candidate: _Candidate) -> tuple[float, int, str]:
    return (
        -candidate.remaining_5h_quota_ratio,
        candidate.circuit_open_count,
        candidate.provider_id,
    )


def _predicted_cost_usd(task: TaskSpec, adapter: ProviderAdapter) -> float:
    return cost_estimator.predict_cost(task, adapter)


def _agent_class_allows_provider(agent_class: str, provider_id: str) -> bool:
    agent_class = agent_class.strip()
    provider_id = _normalise_provider_id(provider_id)
    if provider_id == "anthropic-subscription":
        return agent_class in {"subscription-claude", "api-anthropic"}
    if provider_id == "openai-subscription":
        return agent_class in {"subscription-codex", "api-openai"}
    if provider_id == "gemini-subscription":
        return agent_class in {"subscription-gemini", "api-gemini"}
    if provider_id == "xai-subscription":
        return agent_class in {"subscription-xai", "api-xai"}
    provider_prefix = provider_id.split("-", 1)[0]
    return provider_prefix in agent_class


def _preferred_provider_family_for_task(task: TaskSpec) -> str | None:
    guild = _task_guild(task)
    if guild is None:
        return None
    guild_specs, provider_matrix = _load_model_routing_matrix()
    model_spec = guild_specs.get(guild.value, "")
    provider = _provider_from_model_spec(model_spec)
    if provider in provider_matrix:
        return provider
    return None


def _task_guild(task: TaskSpec) -> Guild | None:
    for attr in ("guild_id", "guild", "agent_name"):
        value = getattr(task, attr, None)
        guild = _coerce_guild(value)
        if guild is not None:
            return guild
    for area in task.area:
        guild = _coerce_guild(area)
        if guild is not None:
            return guild
    return None


def _coerce_guild(value: object) -> Guild | None:
    if isinstance(value, Guild):
        return value
    if not isinstance(value, str):
        return None
    slug = value.strip().lower().replace("-", "_")
    if not slug:
        return None
    try:
        return Guild(slug)
    except ValueError:
        return None


def _load_model_routing_matrix() -> tuple[dict[str, str], set[str]]:
    """Load BP.F guild model mapping + provider matrix for routing order."""

    global _MODEL_ROUTING_CACHE
    try:
        mtime = _MODEL_MAPPING_PATH.stat().st_mtime
    except OSError:
        mtime = None
    if _MODEL_ROUTING_CACHE is not None and _MODEL_ROUTING_CACHE[0] == mtime:
        return _MODEL_ROUTING_CACHE[1], _MODEL_ROUTING_CACHE[2]

    guild_specs: dict[str, str] = {}
    provider_matrix: set[str] = set()
    try:
        if mtime is not None:
            parsed = yaml.safe_load(_MODEL_MAPPING_PATH.read_text(encoding="utf-8")) or {}
            guild_specs, provider_matrix = _parse_model_routing_matrix(parsed)
    except Exception:
        guild_specs, provider_matrix = {}, set()
    _MODEL_ROUTING_CACHE = (mtime, guild_specs, provider_matrix)
    return guild_specs, provider_matrix


def _parse_model_routing_matrix(raw: Any) -> tuple[dict[str, str], set[str]]:
    if not isinstance(raw, dict):
        return {}, set()

    provider_matrix: set[str] = set()
    providers = raw.get("providers")
    if isinstance(providers, dict):
        for provider_id, provider_cfg in providers.items():
            provider = str(provider_id).strip().lower()
            if not provider or not isinstance(provider_cfg, dict):
                continue
            if isinstance(provider_cfg.get("default_model"), str):
                provider_matrix.add(provider)

    guild_specs: dict[str, str] = {}
    guilds = raw.get("guilds")
    if isinstance(guilds, dict):
        for guild_id, cfg in guilds.items():
            guild = _coerce_guild(str(guild_id))
            model_spec = _model_spec_from_mapping_value(cfg)
            if guild is not None and model_spec:
                guild_specs[guild.value] = model_spec
    return guild_specs, provider_matrix


def _model_spec_from_mapping_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        model_spec = value.get("model_spec")
        if isinstance(model_spec, str):
            return model_spec.strip()
    return ""


def _provider_from_model_spec(model_spec: str) -> str | None:
    provider, sep, model = model_spec.partition(":")
    if not sep or not provider.strip() or not model.strip():
        return None
    return provider.strip().lower()


def _provider_family(provider_id: str) -> str:
    return _normalise_provider_id(provider_id).split("-", 1)[0].lower()


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


def is_enabled() -> bool:
    """Return whether multi-provider routing is enabled for this worker."""
    return feature_flags.resolve_env_backed_feature_flag(MP_ENABLED_ENV)


def choose_provider(task: TaskSpec) -> list[ProviderAdapter]:
    """Return ranked provider candidates using the module-default policy."""
    return _DEFAULT_POLICY.choose_provider(task)


def on_cap_hit(provider_id: str, retry_after_s: int | None = None) -> None:
    """Record a cap hit using the module-default policy."""
    _DEFAULT_POLICY.on_cap_hit(provider_id, retry_after_s)


__all__ = [
    "MP_ENABLED_ENV",
    "RoutingPolicy",
    "_recently_capped",
    "choose_provider",
    "is_enabled",
    "on_cap_hit",
]

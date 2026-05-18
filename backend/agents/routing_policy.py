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

import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

import yaml

from backend import feature_flags
from backend.agents import buff_registry
from backend.agents import cost_estimator
from backend.agents import debuff_registry
from backend.agents import difficulty_estimator
from backend.agents import provider_orchestrator
from backend.agents.difficulty_estimator import TaskDifficultyEstimate
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
# RPG.W14.4 (OP-188) — feature flag for the talent-weight injection
# call site. Off by default until the RPG.W14 talent rollout is enabled;
# the helper :func:`talent_routing_weight_multiplier` short-circuits to
# ``1.0`` while the flag is off so the call site is safe to wire today
# and "lights up" the moment the flag flips (typically alongside
# RPG.W7.1 ``prefer_agent_id`` landing).
TALENT_ROUTING_ENABLED_ENV = "OMNISIGHT_MP_TALENT_ROUTING_ENABLED"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MODEL_MAPPING_PATH = _PROJECT_ROOT / "configs" / "model_mapping.yaml"
_ADR_0007_PATH = (
    _PROJECT_ROOT / "docs" / "adr" / "ADR-0007-multi-provider-subscription-orchestrator.md"
)
_ADR_VENDOR_MATRIX_HEADING = "## Vendor capability matrix (for routing policy)"
LOG = logging.getLogger(__name__)

ROUTING_POLICY_PROVIDER_AGENT_CLASS_LABELS = {
    "anthropic": frozenset({"subscription-claude", "api-anthropic"}),
    "openai": frozenset({"subscription-codex", "api-openai"}),
    "gemini": frozenset({"subscription-gemini", "api-gemini"}),
    "xai": frozenset({"subscription-xai", "api-xai"}),
}
ROUTING_POLICY_CONSUMED_PROVIDER_LABELS = frozenset(
    ROUTING_POLICY_PROVIDER_AGENT_CLASS_LABELS
)
ROUTING_POLICY_CONSUMED_AGENT_CLASS_LABELS = frozenset(
    label
    for labels in ROUTING_POLICY_PROVIDER_AGENT_CLASS_LABELS.values()
    for label in labels
)
_ADR_VENDOR_LABEL_ALIASES = {
    "anthropic": "anthropic",
    "claude": "anthropic",
    "openai": "openai",
    "codex": "openai",
    "google": "gemini",
    "gemini": "gemini",
    "xai": "xai",
    "grok": "xai",
}

_recently_capped: dict[str, float] = {}
_RECENTLY_CAPPED_LOCK = RLock()
_MODEL_ROUTING_CACHE: tuple[float | None, dict[str, str], set[str]] | None = None

HumanAssignmentResolver = Callable[[TaskSpec], str | None]
TierGateDecisionResolver = Callable[[TaskSpec, str], object | None]


@dataclass(frozen=True)
class _Candidate:
    adapter: ProviderAdapter
    provider_id: str
    quota_state: QuotaState
    remaining_5h_quota_ratio: float
    circuit_open_count: int
    last_retrained_at: datetime | None


class RoutingPolicy:
    """Choose provider adapters for one task at task-boundary time."""

    def __init__(
        self,
        *,
        orchestrator: object = provider_orchestrator,
        now: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
        human_assignment_resolver: HumanAssignmentResolver | None = None,
        tier_gate_decision_resolver: TierGateDecisionResolver | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._now = now
        self._utcnow = utcnow or _default_utcnow
        self._human_assignment_resolver = (
            human_assignment_resolver or _default_human_assignment_resolver
        )
        self._tier_gate_decision_resolver = tier_gate_decision_resolver

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
            assigned_candidates = [
                candidate
                for candidate in candidates
                if _candidate_matches_assignment(candidate, assigned_provider_id)
            ]
            assigned_eligible = self._tier_gate_eligible_candidates(
                task, assigned_candidates
            )
            if assigned_eligible:
                candidates = assigned_eligible
            elif assigned_candidates:
                decision = self._tier_gate_decision(task, assigned_candidates[0])
                LOG.warning(
                    "routing preferred provider %s under-leveled; "
                    "falling back to eligible candidates (reasons=%s)",
                    assigned_provider_id,
                    getattr(decision, "unmet_reasons", ()),
                )
                candidates = self._tier_gate_eligible_candidates(
                    task,
                    [
                        candidate
                        for candidate in candidates
                        if not _candidate_matches_assignment(
                            candidate, assigned_provider_id
                        )
                    ],
                )
            else:
                candidates = []
        else:
            candidates = self._tier_gate_eligible_candidates(task, candidates)

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
        # OP-676 (L4.9.2): when the Meta-LLM difficulty router is enabled,
        # the per-task difficulty estimate is computed once here and
        # threaded through the sort key so every candidate sees the same
        # provider-family preference multiplier.
        tier = _normalise_tier(task.tier)
        now = self._utcnow()
        difficulty = _difficulty_estimate_for_task(task)
        candidates.sort(
            key=lambda candidate: _tier_sort_key(
                tier, task, candidate, now=now, difficulty=difficulty
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
                    last_retrained_at=_adapter_last_retrained_at(adapter),
                )
            )
        return candidates

    def _tier_gate_eligible_candidates(
        self,
        task: TaskSpec,
        candidates: list[_Candidate],
    ) -> list[_Candidate]:
        eligible: list[_Candidate] = []
        for candidate in candidates:
            decision = self._tier_gate_decision(task, candidate)
            if _tier_x_eligible(candidate, task, decision):
                eligible.append(candidate)
        return eligible

    def _tier_gate_decision(
        self,
        task: TaskSpec,
        candidate: _Candidate,
    ) -> object | None:
        if self._tier_gate_decision_resolver is None:
            return None
        return self._tier_gate_decision_resolver(task, candidate.provider_id)

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


def _tier_sort_key(
    tier: str,
    task: TaskSpec,
    candidate: _Candidate,
    *,
    now: datetime | None = None,
    difficulty: TaskDifficultyEstimate | None = None,
) -> tuple:
    if tier == "S":
        return (
            _predicted_cost_usd(task, candidate.adapter),
            -_routing_priority_score(candidate, now=now, difficulty=difficulty),
            candidate.circuit_open_count,
            candidate.provider_id,
        )
    if tier == "X":
        return (
            -_routing_priority_score(candidate, now=now, difficulty=difficulty),
            candidate.circuit_open_count,
            candidate.provider_id,
        )
    return _quota_first_sort_key(candidate, now=now, difficulty=difficulty)


def _quota_first_sort_key(
    candidate: _Candidate,
    *,
    now: datetime | None = None,
    difficulty: TaskDifficultyEstimate | None = None,
) -> tuple[float, int, str]:
    return (
        -_routing_priority_score(candidate, now=now, difficulty=difficulty),
        candidate.circuit_open_count,
        candidate.provider_id,
    )


def _routing_priority_score(
    candidate: _Candidate,
    *,
    now: datetime | None = None,
    difficulty: TaskDifficultyEstimate | None = None,
) -> float:
    now = now or _default_utcnow()
    base = (
        candidate.remaining_5h_quota_ratio
        * buff_registry.routing_priority_multiplier_for_quota_ratio(
            candidate.remaining_5h_quota_ratio
        )
        * debuff_registry.routing_weight_multiplier_for_last_retrained_at(
            now,
            candidate.last_retrained_at,
        )
    )
    if difficulty is None:
        return base
    family = _provider_family(candidate.provider_id)
    return base * difficulty_estimator.provider_preference_multiplier(
        difficulty.difficulty, family
    )


def _difficulty_estimate_for_task(task: TaskSpec) -> TaskDifficultyEstimate | None:
    """Return the Meta-LLM difficulty estimate for ``task`` when enabled."""
    if not difficulty_estimator.is_enabled():
        return None
    return difficulty_estimator.estimate_difficulty(task)


def _tier_gate_decision_is_eligible(decision: object | None) -> bool:
    if decision is None:
        return True
    return bool(getattr(decision, "eligible", True))


def _tier_x_eligible(
    candidate: _Candidate,
    task: TaskSpec,
    decision: object | None = None,
) -> bool:
    """Return whether ``candidate`` clears the Tier X gate for ``task``."""
    if _normalise_tier(task.tier) != "X":
        return True
    if _tier_gate_decision_is_eligible(decision):
        return True
    LOG.info(
        "tier_x_gate skip provider=%s reasons=%s",
        candidate.provider_id,
        getattr(decision, "unmet_reasons", ()),
    )
    return False


def _predicted_cost_usd(task: TaskSpec, adapter: ProviderAdapter) -> float:
    return cost_estimator.predict_cost(task, adapter)


def _agent_class_allows_provider(agent_class: str, provider_id: str) -> bool:
    agent_class = agent_class.strip()
    provider_id = _normalise_provider_id(provider_id)
    provider_prefix = provider_id.split("-", 1)[0]
    labels = ROUTING_POLICY_PROVIDER_AGENT_CLASS_LABELS.get(provider_prefix)
    if labels is not None and provider_id == f"{provider_prefix}-subscription":
        return agent_class in labels
    return provider_prefix in agent_class


def adr_0007_vendor_capability_labels(
    adr_path: Path = _ADR_0007_PATH,
) -> frozenset[str]:
    """Return provider-family labels declared by ADR 0007's vendor matrix."""
    try:
        body = adr_path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()

    lines = body.splitlines()
    try:
        start = lines.index(_ADR_VENDOR_MATRIX_HEADING)
    except ValueError:
        return frozenset()

    labels: set[str] = set()
    in_table = False
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            if in_table:
                break
            continue
        if not stripped.startswith("|"):
            if in_table:
                break
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells or cells[0] in {"Provider", "---"}:
            in_table = True
            continue
        label = _adr_vendor_label(cells[0])
        if label is not None:
            labels.add(label)
        in_table = True
    return frozenset(labels)


def assert_adr_0007_vendor_capability_matrix_consumed(
    adr_path: Path = _ADR_0007_PATH,
) -> None:
    """Drift guard: every ADR 0007 matrix vendor is consumed by routing."""
    adr_labels = adr_0007_vendor_capability_labels(adr_path)
    if not adr_labels:
        raise AssertionError(
            "ADR 0007 vendor capability matrix is missing or empty at "
            f"{adr_path}"
        )
    missing = adr_labels - ROUTING_POLICY_CONSUMED_PROVIDER_LABELS
    if missing:
        raise AssertionError(
            "ADR 0007 vendor capability matrix has unconsumed routing labels: "
            f"{sorted(missing)!r}; consumed labels are "
            f"{sorted(ROUTING_POLICY_CONSUMED_PROVIDER_LABELS)!r}"
        )


def _adr_vendor_label(raw: str) -> str | None:
    tokens = re.findall(r"[a-z0-9]+", raw.lower())
    for token in tokens:
        label = _ADR_VENDOR_LABEL_ALIASES.get(token)
        if label is not None:
            return label
    return None


def _preferred_provider_family_for_task(task: TaskSpec) -> str | None:
    guild = _task_guild(task)
    if guild is None:
        return None
    guild_specs, provider_matrix = _load_model_routing_matrix()
    model_spec = guild_specs.get(guild.value, "")
    provider = _provider_from_model_spec(model_spec)
    provider_family = _adr_vendor_label(provider or "")
    if provider_family in provider_matrix:
        return provider_family
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
            provider_family = _adr_vendor_label(provider)
            if provider_family is None:
                continue
            if isinstance(provider_cfg.get("default_model"), str):
                provider_matrix.add(provider_family)

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


def _candidate_matches_assignment(
    candidate: _Candidate,
    assigned_agent_id: str,
) -> bool:
    return (
        _normalise_provider_id(assigned_agent_id)
        in _candidate_assignment_ids(candidate)
    )


def _candidate_assignment_ids(candidate: _Candidate) -> frozenset[str]:
    values = {candidate.provider_id}
    values.update(_adapter_identity_values(candidate.adapter))
    return frozenset(
        _normalise_provider_id(value) for value in values if value.strip()
    )


def _adapter_identity_values(adapter: ProviderAdapter) -> set[str]:
    values: set[str] = set()
    for attr in (
        "agent_id",
        "adapter_id",
        "instance_id",
        "runner_instance_id",
    ):
        value = getattr(adapter, attr, None)
        try:
            candidate = value() if callable(value) else value
        except Exception:
            candidate = None
        if isinstance(candidate, str) and candidate.strip():
            values.add(candidate)

    metadata = getattr(adapter, "metadata", None)
    if callable(metadata):
        try:
            metadata = metadata()
        except Exception:
            metadata = None
    if isinstance(metadata, dict):
        for key in (
            "agent_id",
            "adapter_id",
            "instance_id",
            "runner_instance_id",
        ):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                values.add(value)
    return values


def _adapter_last_retrained_at(adapter: ProviderAdapter) -> datetime | None:
    for attr in (
        "last_retrained_at",
        "last_retrain_at",
        "last_training_at",
        "fine_tuned_at",
    ):
        value = getattr(adapter, attr, None)
        try:
            candidate = value() if callable(value) else value
        except Exception:
            candidate = None
        parsed = _coerce_datetime(candidate)
        if parsed is not None:
            return parsed

    metadata = getattr(adapter, "metadata", None)
    if callable(metadata):
        try:
            metadata = metadata()
        except Exception:
            metadata = None
    if isinstance(metadata, dict):
        for key in (
            "last_retrained_at",
            "last_retrain_at",
            "last_training_at",
            "fine_tuned_at",
        ):
            parsed = _coerce_datetime(metadata.get(key))
            if parsed is not None:
                return parsed
    return None


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _default_utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def is_talent_routing_enabled() -> bool:
    """Return whether the RPG.W14.4 (OP-188) talent-weight injection is active.

    Feature-flagged off by default. Enabling the flag activates the
    +20%-per-matching-talent multiplier from
    :mod:`backend.agents.talent_tree`.
    """
    return feature_flags.resolve_env_backed_feature_flag(
        TALENT_ROUTING_ENABLED_ENV,
        default_enabled=False,
        env_mode="true_values",
    )


def talent_routing_weight_multiplier(
    talent_choices: tuple,
    *,
    task_labels: tuple[str, ...],
    guild: str | None = None,
) -> float:
    """RPG.W14.4 (OP-188) -- routing-weight multiplier for ``talent_choices``.

    Returns ``1.0`` unconditionally when
    :func:`is_talent_routing_enabled` is False, so the call site is
    safe to wire today and "lights up" the moment the feature flag
    flips. ``MP routing_policy unreachable`` (RoutingWeightInjectionFailed)
    degrades silently to ``1.0`` per OP-219's error-catalog spec.

    Production callers that need to look up the agent's choices first
    should reach for :func:`build_talent_routing_weight_resolver`
    instead of composing ``store.list_choices`` + this helper
    themselves.
    """
    if not is_talent_routing_enabled():
        return 1.0
    if not talent_choices:
        return 1.0
    try:
        # Imported lazily to keep the module import graph clean — W14
        # is a leaf feature and routing_policy is itself imported by
        # many other modules at startup.
        from backend.agents.talent_tree import (
            RoutingWeightInjectionFailed,
            TalentTreeError,
            routing_weight_multiplier_for_talents,
        )
    except ImportError:  # pragma: no cover — defensive
        return 1.0
    try:
        return routing_weight_multiplier_for_talents(
            tuple(talent_choices),
            task_labels=tuple(task_labels),
            guild=guild,
        )
    except (OSError, RoutingWeightInjectionFailed, TalentTreeError):
        return 1.0


def build_talent_routing_weight_resolver(
    store: Any,
    *,
    path: Path | str | None = None,
) -> Callable[..., Any]:
    """W14.4 (OP-188) -- closure that resolves the per-agent routing multiplier.

    The returned closure has the shape
    ``async (agent_id, *, task_labels, guild=None) -> float`` and is
    the production-wiring surface for MP dispatch code that wants the
    talent-weight bump without hand-rolling the
    :meth:`TalentChoiceStore.list_choices` lookup:

    .. code-block:: python

        from backend.agents.routing_policy import (
            build_talent_routing_weight_resolver,
        )
        from backend.agents.talent_tree import PostgresTalentChoiceStore

        store = PostgresTalentChoiceStore(conn_factory)
        resolve = build_talent_routing_weight_resolver(store)

        bump = await resolve(
            agent_id="agent-A",
            task_labels=("security", "schema"),
            guild=Guild.backend,
        )
        weighted_score = base_score * bump

    The closure honours :func:`is_talent_routing_enabled` and degrades
    silently to ``1.0`` on:

    * the feature flag being off,
    * an empty / missing per-agent talent set,
    * any :class:`backend.agents.talent_tree.RoutingWeightInjectionFailed`
      raised by the YAML loader (the W14.4 error-catalog contract),
    * any other unexpected error inside ``store.list_choices`` -- the
      MP dispatch path must never crash because the talent layer is
      unreachable.

    ``path`` is forwarded to
    :func:`backend.agents.talent_tree.routing_weight_multiplier_for_talents`
    so unit tests can pin the YAML to a fixture; production callers
    leave it ``None`` and the talent_tree default is used.
    """
    from backend.agents.talent_tree import (
        RoutingWeightInjectionFailed,
        TALENT_TREE_PATH,
        routing_weight_multiplier_for_talents,
    )

    resolved_path = path if path is not None else TALENT_TREE_PATH

    async def _resolve(
        agent_id: str,
        *,
        task_labels: tuple[str, ...],
        guild: Any = None,
    ) -> float:
        if not is_talent_routing_enabled():
            return 1.0
        try:
            choices = await store.list_choices(agent_id)
        except Exception:  # noqa: BLE001 -- degrade-silently contract
            return 1.0
        if not choices:
            return 1.0
        try:
            return routing_weight_multiplier_for_talents(
                tuple(choices),
                task_labels=tuple(task_labels),
                path=resolved_path,
                guild=guild,
            )
        except RoutingWeightInjectionFailed:
            return 1.0

    return _resolve
def capstone_routing_weight_multiplier(
    capstone_lock,
    *,
    task_labels: tuple[str, ...],
    guild: str | None = None,
) -> float:
    """RPG.W14.6 -- routing weight multiplier for a Lv-80 capstone lock.

    Returns ``1.0`` when :func:`is_talent_routing_enabled` is False
    (shares the W14 feature flag — capstone routing rides the same
    rollout switch as per-talent routing so operators flip one env
    var, not two). ``capstone_lock`` may be ``None`` (agent below
    Lv 80) — also returns ``1.0``. The actual multiplier
    (:data:`~backend.agents.talent_tree.ROUTING_WEIGHT_CAPSTONE_MATCH`,
    ``+50%``) is applied only when the capstone's ``signature_label``
    matches one of ``task_labels``.

    YAML / IO failures degrade silently to ``1.0`` (mirrors
    :func:`talent_routing_weight_multiplier`).
    """
    if not is_talent_routing_enabled():
        return 1.0
    if capstone_lock is None:
        return 1.0
    try:
        from backend.agents.talent_tree import (
            RoutingWeightInjectionFailed,
            capstone_routing_weight_multiplier as _impl,
        )
    except ImportError:  # pragma: no cover — defensive
        return 1.0
    try:
        return _impl(
            capstone_lock,
            task_labels=tuple(task_labels),
            guild=guild,
        )
    except RoutingWeightInjectionFailed:
        return 1.0


def choose_provider(task: TaskSpec) -> list[ProviderAdapter]:
    """Return ranked provider candidates using the module-default policy."""
    return _DEFAULT_POLICY.choose_provider(task)


def on_cap_hit(provider_id: str, retry_after_s: int | None = None) -> None:
    """Record a cap hit using the module-default policy."""
    _DEFAULT_POLICY.on_cap_hit(provider_id, retry_after_s)


__all__ = [
    "MP_ENABLED_ENV",
    "RoutingPolicy",
    "TALENT_ROUTING_ENABLED_ENV",
    "_recently_capped",
    "build_talent_routing_weight_resolver",
    "choose_provider",
    "is_enabled",
    "is_talent_routing_enabled",
    "on_cap_hit",
    "talent_routing_weight_multiplier",
]

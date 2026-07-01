"""OP-1115 capability profile overlay for runner capability resolution."""
from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from backend.agents import capability_matrix, provider_quota_tracker
from backend.agents.provider_quota_tracker import QuotaState


LOG = logging.getLogger(__name__)

QUOTA_EXHAUSTED_HEALTH_STATE = "quota-exhausted"
TIER_ORDER: Mapping[str, int] = {"S": 0, "M": 1, "L": 2, "X": 3}
AGENT_CLASS_PROFILE: Mapping[str, tuple[str, str]] = {
    "subscription-codex": ("openai-subscription", "<unknown>"),
    "api-openai": ("openai-subscription", "<unknown>"),
    "subscription-claude": ("anthropic-subscription", "<unknown>"),
    "api-anthropic": ("anthropic-subscription", "<unknown>"),
    # Gemini/Antigravity brain (dogfood 2026-07-01).
    "subscription-gemini": ("gemini-subscription", "<unknown>"),
}


class CapabilityRegistryError(RuntimeError):
    """Base class for capability registry adapter failures."""


class CapabilityRegistryDBError(CapabilityRegistryError):
    """The capability_profile lookup failed."""


class CapabilityRegistryDenied(CapabilityRegistryError):
    """A profile exists but denies this pickup."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        profile_id: str,
        reason: str,
    ) -> None:
        self.provider = provider
        self.model = model
        self.profile_id = profile_id
        self.reason = reason
        super().__init__(
            f"capability_profile denied provider={provider!r} model={model!r} "
            f"profile_id={profile_id!r}: {reason}"
        )


@dataclass(frozen=True)
class CapabilityProfile:
    profile_id: str
    provider: str
    model: str
    tools: frozenset[str]
    max_tier: str
    cost_mode: str
    health_state: str
    active: bool = True


def profile_key_from_labels(labels: Iterable[str]) -> tuple[str, str] | None:
    """Map legacy ``class:<agent_class>`` labels to a profile lookup key."""
    for label in labels:
        if not isinstance(label, str) or not label.startswith("class:"):
            continue
        agent_class = label.removeprefix("class:")
        profile_key = AGENT_CLASS_PROFILE.get(agent_class)
        if profile_key is not None:
            return profile_key
    return None


async def lookup_active_profile(
    conn: Any,
    provider: str,
    model: str,
) -> CapabilityProfile | None:
    """Fetch one active profile by ``(provider, model)`` from Postgres."""
    row = await _maybe_await(
        conn.fetchrow(
            """
            SELECT profile_id, provider, model, tools, max_tier, cost_mode,
                   health_state, active
            FROM capability_profile
            WHERE provider = $1
              AND model = $2
              AND active = TRUE
            """,
            provider,
            model,
        )
    )
    if row is None:
        return None
    return profile_from_row(row)


def profile_from_row(row: Mapping[str, Any] | Any) -> CapabilityProfile:
    tools_raw = _row_get(row, "tools")
    if isinstance(tools_raw, str):
        tools_raw = json.loads(tools_raw)
    tools = frozenset(capability_matrix._clean_capability(c) for c in tools_raw)
    unknown = tools - capability_matrix.CAPABILITIES
    if unknown:
        raise capability_matrix.CapabilityMatrixError(
            f"capability_profile.tools references unknown capabilities: {sorted(unknown)}"
        )
    return CapabilityProfile(
        profile_id=str(_row_get(row, "profile_id")),
        provider=str(_row_get(row, "provider")),
        model=str(_row_get(row, "model")),
        tools=tools,
        max_tier=str(_row_get(row, "max_tier")),
        cost_mode=str(_row_get(row, "cost_mode")),
        health_state=str(_row_get(row, "health_state")),
        active=bool(_row_get(row, "active")),
    )


def effective_health_state(
    profile: CapabilityProfile,
    *,
    quota_state_getter: Callable[[str], QuotaState] | None = None,
) -> str:
    """Return profile health with provider quota exhaustion folded in."""
    if profile.health_state in {"down", QUOTA_EXHAUSTED_HEALTH_STATE}:
        return profile.health_state
    if quota_state_getter is None:
        quota_state_getter = provider_quota_tracker.get_quota_state
    try:
        quota_state = quota_state_getter(profile.provider)
    except Exception as exc:  # noqa: BLE001 - quota telemetry must fail open
        LOG.warning(
            "capability_registry.quota_state_unavailable provider=%s model=%s err=%s",
            profile.provider,
            profile.model,
            exc,
        )
        return profile.health_state
    if provider_quota_tracker.quota_state_exhausted(quota_state):
        return QUOTA_EXHAUSTED_HEALTH_STATE
    return profile.health_state


def quota_health_denial_from_labels(
    labels: Iterable[str],
    *,
    quota_state_getter: Callable[[str], QuotaState] | None = None,
) -> str | None:
    """Return a pickup denial reason when legacy class labels map to exhausted quota."""
    profile_key = profile_key_from_labels(labels)
    if profile_key is None:
        return None
    provider, model = profile_key
    profile = CapabilityProfile(
        profile_id=f"{provider}:{model}",
        provider=provider,
        model=model,
        tools=frozenset(),
        max_tier="X",
        cost_mode="unknown",
        health_state="healthy",
    )
    if (
        effective_health_state(profile, quota_state_getter=quota_state_getter)
        != QUOTA_EXHAUSTED_HEALTH_STATE
    ):
        return None
    return f"capability_profile.health:{QUOTA_EXHAUSTED_HEALTH_STATE}:{provider}"


async def resolve(
    matrix: capability_matrix.CapabilityMatrix,
    *,
    conn: Any,
    ticket_type: str,
    areas: Iterable[str],
    tier: str,
    labels: Iterable[str] = (),
    provider: str | None = None,
    model: str | None = None,
) -> frozenset[str]:
    """Resolve capabilities using the DB profile first, then legacy labels.

    The registry narrows the OP-855 matrix. If the profile is missing, the
    read-only default is returned with label overrides applied. If the lookup
    itself fails, callers get the matrix-only behaviour and a loud warning.
    """
    label_list = list(labels)
    if provider is None or model is None:
        profile_key = profile_key_from_labels(label_list)
        if profile_key is None:
            return matrix.resolve_for_areas(ticket_type, areas, tier, labels=label_list)
        provider, model = profile_key

    try:
        profile = await lookup_active_profile(conn, provider, model)
    except Exception as exc:  # noqa: BLE001 - rollout must not break pickup
        LOG.warning(
            "capability_registry.db_unreachable provider=%s model=%s err=%s",
            provider,
            model,
            exc,
        )
        return matrix.resolve_for_areas(ticket_type, areas, tier, labels=label_list)

    if profile is None:
        LOG.warning(
            "capability_registry.missing_profile provider=%s model=%s",
            provider,
            model,
        )
        return capability_matrix.apply_label_overrides(
            matrix.read_only_default, label_list
        )

    return capability_matrix.resolve_for_areas_with_profile(
        matrix,
        ticket_type,
        areas,
        tier,
        profile=profile,
        labels=label_list,
    )


def _enforce_profile(profile: CapabilityProfile, tier: str) -> None:
    max_rank = TIER_ORDER.get(profile.max_tier)
    tier_rank = TIER_ORDER.get(tier)
    if max_rank is None or tier_rank is None:
        raise CapabilityRegistryDenied(
            provider=profile.provider,
            model=profile.model,
            profile_id=profile.profile_id,
            reason=f"unknown tier max_tier={profile.max_tier!r} tier={tier!r}",
        )
    if tier_rank > max_rank:
        raise CapabilityRegistryDenied(
            provider=profile.provider,
            model=profile.model,
            profile_id=profile.profile_id,
            reason=f"tier {tier!r} exceeds max_tier {profile.max_tier!r}",
        )
    health_state = effective_health_state(profile)
    if health_state == "down":
        raise CapabilityRegistryDenied(
            provider=profile.provider,
            model=profile.model,
            profile_id=profile.profile_id,
            reason="health_state is down",
        )
    if health_state == QUOTA_EXHAUSTED_HEALTH_STATE:
        raise CapabilityRegistryDenied(
            provider=profile.provider,
            model=profile.model,
            profile_id=profile.profile_id,
            reason=f"health_state is {QUOTA_EXHAUSTED_HEALTH_STATE}",
        )
    if health_state == "degraded":
        LOG.warning(
            "capability_registry.degraded_profile provider=%s model=%s profile_id=%s",
            profile.provider,
            profile.model,
            profile.profile_id,
        )


def _row_get(row: Mapping[str, Any] | Any, key: str) -> Any:
    try:
        return row[key]
    except TypeError:
        return getattr(row, key)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = [
    "AGENT_CLASS_PROFILE",
    "CapabilityProfile",
    "CapabilityRegistryDBError",
    "CapabilityRegistryDenied",
    "CapabilityRegistryError",
    "QUOTA_EXHAUSTED_HEALTH_STATE",
    "effective_health_state",
    "lookup_active_profile",
    "profile_from_row",
    "profile_key_from_labels",
    "quota_health_denial_from_labels",
    "resolve",
]

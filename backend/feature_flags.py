"""WP.7.2 / WP.7.3 -- feature flag registry primitives.

This module is declarative only: it defines the legal tier / state
labels that may appear in ``feature_flags`` and the frozen metadata
needed by later WP.7 rows. Runtime cache invalidation, expiry
enforcement, and operator toggles remain separate rows.

Module-global state audit
-------------------------
Qualified answer #1 of ``docs/sop/implement_phase_step.md``: every
worker derives the same immutable values from code at import time. The
enum, tuple, frozenset, and mapping-proxy constants below are not caches
and are not mutated at runtime, so multi-worker consistency is trivial.
"""

from __future__ import annotations

from enum import Enum
from types import MappingProxyType
from typing import Mapping, NamedTuple


class FeatureFlagTier(str, Enum):
    """Five deployment tiers for rows in ``feature_flags``.

    Enum member names mirror the product language (DEBUG / DOGFOOD /
    PREVIEW / RELEASE / RUNTIME). String values are lowercase because
    they are the stable DB / JSON / log labels used by WP.7.1 and later
    runtime code.
    """

    DEBUG = "debug"
    DOGFOOD = "dogfood"
    PREVIEW = "preview"
    RELEASE = "release"
    RUNTIME = "runtime"

    @classmethod
    def parse(cls, raw: str | "FeatureFlagTier") -> "FeatureFlagTier":
        """Return a tier for exact lowercase DB labels or enum members."""
        if isinstance(raw, cls):
            return raw
        return cls(str(raw).strip().lower())


class FeatureFlagTierDefinition(NamedTuple):
    """Operator-facing definition for one feature flag tier."""

    label: str
    audience: str
    purpose: str


class FeatureFlagState(str, Enum):
    """Two global / preference states for feature flag resolution."""

    DISABLED = "disabled"
    ENABLED = "enabled"

    @classmethod
    def parse(cls, raw: str | "FeatureFlagState") -> "FeatureFlagState":
        """Return a state for exact lowercase DB labels or enum members."""
        if isinstance(raw, cls):
            return raw
        return cls(str(raw).strip().lower())


class FeatureFlagResolutionSource(str, Enum):
    """Source that supplied the winning value during flag resolution."""

    TEST_OVERRIDE = "test_override"
    USER_PREFERENCE = "user_preference"
    GLOBAL_STATE = "global_state"
    DEFAULT = "default"


class FeatureFlagResolution(NamedTuple):
    """Resolved feature flag state plus the winning source."""

    state: FeatureFlagState
    source: FeatureFlagResolutionSource


FEATURE_FLAG_TIER_ORDER: tuple[FeatureFlagTier, ...] = (
    FeatureFlagTier.DEBUG,
    FeatureFlagTier.DOGFOOD,
    FeatureFlagTier.PREVIEW,
    FeatureFlagTier.RELEASE,
    FeatureFlagTier.RUNTIME,
)

FEATURE_FLAG_TIER_VALUES: tuple[str, ...] = tuple(
    tier.value for tier in FEATURE_FLAG_TIER_ORDER
)

FEATURE_FLAG_TIER_VALUE_SET: frozenset[str] = frozenset(
    FEATURE_FLAG_TIER_VALUES
)

FEATURE_FLAG_STATE_VALUES: tuple[str, ...] = tuple(
    state.value for state in FeatureFlagState
)

FEATURE_FLAG_STATE_VALUE_SET: frozenset[str] = frozenset(
    FEATURE_FLAG_STATE_VALUES
)

FEATURE_FLAG_RESOLUTION_SOURCE_ORDER: tuple[FeatureFlagResolutionSource, ...] = (
    FeatureFlagResolutionSource.TEST_OVERRIDE,
    FeatureFlagResolutionSource.USER_PREFERENCE,
    FeatureFlagResolutionSource.GLOBAL_STATE,
    FeatureFlagResolutionSource.DEFAULT,
)

FEATURE_FLAG_TIER_DEFINITIONS: Mapping[
    FeatureFlagTier, FeatureFlagTierDefinition
] = MappingProxyType({
    FeatureFlagTier.DEBUG: FeatureFlagTierDefinition(
        label="DEBUG",
        audience="dev-only",
        purpose="Development-only feature testing",
    ),
    FeatureFlagTier.DOGFOOD: FeatureFlagTierDefinition(
        label="DOGFOOD",
        audience="internal + early-access",
        purpose="Internal dogfood and early-access cohort",
    ),
    FeatureFlagTier.PREVIEW: FeatureFlagTierDefinition(
        label="PREVIEW",
        audience="external tester",
        purpose="Beta program customers",
    ),
    FeatureFlagTier.RELEASE: FeatureFlagTierDefinition(
        label="RELEASE",
        audience="GA",
        purpose="Generally available customer-facing flag",
    ),
    FeatureFlagTier.RUNTIME: FeatureFlagTierDefinition(
        label="RUNTIME",
        audience="server-pushed",
        purpose="Server-pushed flag adjustable without redeploy",
    ),
})


def is_feature_flag_tier(value: str | FeatureFlagTier) -> bool:
    """Return True when ``value`` is one of the five canonical tiers."""
    if isinstance(value, FeatureFlagTier):
        return True
    return str(value).strip().lower() in FEATURE_FLAG_TIER_VALUE_SET


def is_feature_flag_state(value: str | FeatureFlagState) -> bool:
    """Return True when ``value`` is one of the two canonical states."""
    if isinstance(value, FeatureFlagState):
        return True
    return str(value).strip().lower() in FEATURE_FLAG_STATE_VALUE_SET


def resolve_feature_flag_state(
    *,
    default: str | FeatureFlagState,
    global_state: str | FeatureFlagState | None = None,
    user_preference: str | FeatureFlagState | None = None,
    test_override: str | FeatureFlagState | None = None,
) -> FeatureFlagResolution:
    """Resolve a flag using WP.7.3 priority order.

    Priority is intentionally data-only and cache-free:
    test_override -> user_preference -> global_state -> default.
    Multi-worker consistency is inherited from the caller-provided
    sources; this helper stores no module-global mutable state.
    """
    candidates: tuple[
        tuple[FeatureFlagResolutionSource, str | FeatureFlagState | None],
        ...,
    ] = (
        (FeatureFlagResolutionSource.TEST_OVERRIDE, test_override),
        (FeatureFlagResolutionSource.USER_PREFERENCE, user_preference),
        (FeatureFlagResolutionSource.GLOBAL_STATE, global_state),
        (FeatureFlagResolutionSource.DEFAULT, default),
    )
    for source, raw_state in candidates:
        if raw_state is not None:
            return FeatureFlagResolution(
                state=FeatureFlagState.parse(raw_state),
                source=source,
            )
    raise AssertionError("default feature flag state must not be None")


__all__ = [
    "FEATURE_FLAG_RESOLUTION_SOURCE_ORDER",
    "FEATURE_FLAG_STATE_VALUES",
    "FEATURE_FLAG_STATE_VALUE_SET",
    "FEATURE_FLAG_TIER_DEFINITIONS",
    "FEATURE_FLAG_TIER_ORDER",
    "FEATURE_FLAG_TIER_VALUES",
    "FEATURE_FLAG_TIER_VALUE_SET",
    "FeatureFlagResolution",
    "FeatureFlagResolutionSource",
    "FeatureFlagState",
    "FeatureFlagTier",
    "FeatureFlagTierDefinition",
    "is_feature_flag_state",
    "is_feature_flag_tier",
    "resolve_feature_flag_state",
]

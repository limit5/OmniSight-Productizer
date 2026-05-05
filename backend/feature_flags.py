"""WP.7.2 -- tier definitions for the feature flag registry.

This module is declarative only: it defines the five legal tier labels
that may appear in ``feature_flags.tier`` and the frozen metadata needed
by later WP.7 rows. Runtime resolution priority, cache invalidation,
expiry enforcement, and operator toggles remain separate rows.

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


__all__ = [
    "FEATURE_FLAG_TIER_DEFINITIONS",
    "FEATURE_FLAG_TIER_ORDER",
    "FEATURE_FLAG_TIER_VALUES",
    "FEATURE_FLAG_TIER_VALUE_SET",
    "FeatureFlagTier",
    "FeatureFlagTierDefinition",
    "is_feature_flag_tier",
]

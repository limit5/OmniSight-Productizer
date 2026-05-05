"""WP.7.2-WP.7.5 -- feature flag registry primitives.

This module is declarative only: it defines the legal tier / state
labels that may appear in ``feature_flags`` and the frozen metadata
needed by later WP.7 rows. Runtime expiry enforcement and operator
toggles remain separate rows.

Module-global state audit
-------------------------
The enum, tuple, frozenset, and mapping-proxy constants use qualified
answer #1 of ``docs/sop/implement_phase_step.md``: every worker derives
the same immutable values from code at import time.

WP.7.4 adds ``default_feature_flag_registry`` as a process-local
in-memory cache for hot-path global-state reads. Cross-worker
consistency uses qualified answer #2: writers call
``publish_feature_flags_invalidate()``, which clears this worker and
broadcasts ``FEATURE_FLAGS_INVALIDATE_EVENT`` over Redis pub/sub so peer
workers clear their local snapshots. Without Redis, invalidation is
local-only and intentionally a single-worker/dev fallback.

WP.7.5 expiry enforcement is data-only: every worker derives the same
expired-flag verdict from the same registry snapshot plus caller-supplied
CI clock. It does not mutate cache state.

WP.7.7 keeps env-backed rollback knobs as registry-first / env-fallback
reads. Cross-worker consistency follows the registry cache contract when
a row exists; otherwise every worker derives the fallback from the same
process environment.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import threading
import time
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, NamedTuple


logger = logging.getLogger(__name__)


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


class FeatureFlagRecord(NamedTuple):
    """One immutable row from ``feature_flags`` used by hot-path reads."""

    flag_name: str
    tier: FeatureFlagTier
    state: FeatureFlagState
    expires_at: str | None = None
    owner: str = ""
    created_at: str = ""


class FeatureFlagEnvKnob(NamedTuple):
    """One existing env rollback knob progressively folded into WP.7."""

    env_name: str
    flag_name: str
    tier: FeatureFlagTier
    default_state: FeatureFlagState
    owner: str


class FeatureFlagExpiryViolation(NamedTuple):
    """One expired feature flag that must be cleaned before CI passes."""

    flag_name: str
    tier: FeatureFlagTier
    state: FeatureFlagState
    expires_at: str
    owner: str


class FeatureFlagRegistrySnapshot(NamedTuple):
    """Immutable feature-flag snapshot swapped atomically as one object."""

    flags: Mapping[str, FeatureFlagRecord]
    loaded_at: float


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

FEATURE_FLAG_ENV_PREFIXES: tuple[str, ...] = (
    "OMNISIGHT_BP_",
    "OMNISIGHT_HD_",
    "OMNISIGHT_KS_",
    "OMNISIGHT_WP_",
)

FEATURE_FLAG_ENV_TRUE_VALUES: frozenset[str] = frozenset(
    {"1", "true", "yes", "on"}
)

FEATURE_FLAG_ENV_FALSE_VALUES: frozenset[str] = frozenset(
    {"0", "false", "no", "off"}
)

FEATURE_FLAG_ENV_KNOBS: Mapping[str, FeatureFlagEnvKnob] = MappingProxyType({
    "OMNISIGHT_KS_ENVELOPE_ENABLED": FeatureFlagEnvKnob(
        env_name="OMNISIGHT_KS_ENVELOPE_ENABLED",
        flag_name="ks.envelope.enabled",
        tier=FeatureFlagTier.RELEASE,
        default_state=FeatureFlagState.ENABLED,
        owner="ks",
    ),
    "OMNISIGHT_KS_CMEK_ENABLED": FeatureFlagEnvKnob(
        env_name="OMNISIGHT_KS_CMEK_ENABLED",
        flag_name="ks.cmek.enabled",
        tier=FeatureFlagTier.PREVIEW,
        default_state=FeatureFlagState.ENABLED,
        owner="ks",
    ),
    "OMNISIGHT_KS_BYOG_ENABLED": FeatureFlagEnvKnob(
        env_name="OMNISIGHT_KS_BYOG_ENABLED",
        flag_name="ks.byog.enabled",
        tier=FeatureFlagTier.PREVIEW,
        default_state=FeatureFlagState.ENABLED,
        owner="ks",
    ),
    "OMNISIGHT_WP_DIFF_VALIDATION_ENABLED": FeatureFlagEnvKnob(
        env_name="OMNISIGHT_WP_DIFF_VALIDATION_ENABLED",
        flag_name="wp.diff_validation.enabled",
        tier=FeatureFlagTier.RELEASE,
        default_state=FeatureFlagState.ENABLED,
        owner="wp",
    ),
    "OMNISIGHT_WP_SKILLS_LOADER_ENABLED": FeatureFlagEnvKnob(
        env_name="OMNISIGHT_WP_SKILLS_LOADER_ENABLED",
        flag_name="wp.skills_loader.enabled",
        tier=FeatureFlagTier.RELEASE,
        default_state=FeatureFlagState.ENABLED,
        owner="wp",
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


def is_feature_flag_env_knob(env_name: str) -> bool:
    """Return True for BP / HD / KS / WP env knobs managed by WP.7."""
    return str(env_name).strip().startswith(FEATURE_FLAG_ENV_PREFIXES)


def feature_flag_name_for_env_knob(env_name: str) -> str:
    """Return the canonical registry flag name for one env knob.

    Known knobs use the pinned WP.7.7 manifest. New BP / HD / KS / WP
    knobs can be folded in progressively with the same mechanical name:
    ``OMNISIGHT_KS_CMEK_ENABLED`` -> ``ks.cmek.enabled``.
    """
    key = str(env_name).strip()
    if key in FEATURE_FLAG_ENV_KNOBS:
        return FEATURE_FLAG_ENV_KNOBS[key].flag_name
    for prefix in FEATURE_FLAG_ENV_PREFIXES:
        if key.startswith(prefix):
            return key[len("OMNISIGHT_"):].lower().replace("_", ".")
    raise ValueError(f"env knob is outside WP.7 registry prefixes: {env_name!r}")


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


def resolve_env_backed_feature_flag(
    env_name: str,
    *,
    default_enabled: bool = True,
    registry: FeatureFlagRegistry | None = None,
    env_mode: str = "disabled_values",
) -> bool:
    """Resolve a WP.7 env-backed flag as registry row, then env fallback.

    The registry global state is authoritative when present; env remains
    only the migration fallback so existing deployments keep their
    rollback knobs until rows are seeded in ``feature_flags``.
    """
    global_state = get_feature_flag_global_state(
        feature_flag_name_for_env_knob(env_name),
        registry=registry,
    )
    if global_state is not None:
        return global_state is FeatureFlagState.ENABLED

    raw = os.environ.get(env_name)
    if raw is None:
        return default_enabled
    value = raw.strip().lower()
    if env_mode == "true_values":
        return value in FEATURE_FLAG_ENV_TRUE_VALUES
    if env_mode != "disabled_values":
        raise ValueError(f"unknown env_mode={env_mode!r}")
    return value not in FEATURE_FLAG_ENV_FALSE_VALUES


def _parse_expires_at(raw: str) -> datetime:
    value = raw.strip()
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def find_expired_feature_flags(
    records: Iterable[FeatureFlagRecord],
    *,
    now: datetime | None = None,
) -> tuple[FeatureFlagExpiryViolation, ...]:
    """Return feature flags whose ``expires_at`` is earlier than ``now``.

    ``expires_at=None`` means the flag has no expiry to enforce yet; this
    helper only fails stale rows that have crossed their declared cleanup
    deadline. CI callers should pass one registry snapshot so every
    worker reaches the same verdict from the same source data.
    """
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    else:
        clock = clock.astimezone(timezone.utc)

    violations: list[FeatureFlagExpiryViolation] = []
    for record in records:
        if record.expires_at is None:
            continue
        expires_at = _parse_expires_at(record.expires_at)
        if expires_at < clock:
            violations.append(
                FeatureFlagExpiryViolation(
                    flag_name=record.flag_name,
                    tier=record.tier,
                    state=record.state,
                    expires_at=record.expires_at,
                    owner=record.owner,
                )
            )
    return tuple(violations)


def assert_no_expired_feature_flags(
    records: Iterable[FeatureFlagRecord],
    *,
    now: datetime | None = None,
) -> None:
    """Fail close when any feature flag has passed ``expires_at``."""
    violations = find_expired_feature_flags(records, now=now)
    if not violations:
        return
    details = ", ".join(
        f"{v.flag_name} (tier={v.tier.value}, owner={v.owner or '<unset>'}, "
        f"expires_at={v.expires_at})"
        for v in violations
    )
    raise AssertionError(
        "expired feature flags must be cleaned before CI passes: "
        f"{details}"
    )


FeatureFlagLoader = Callable[[], Iterable[FeatureFlagRecord | Mapping[str, Any]]]


class FeatureFlagRegistry:
    """Hot-path in-memory cache for global ``feature_flags`` state.

    Reads take the current immutable snapshot and avoid Redis / DB work
    after the first load. Reloads build a new mapping-proxy snapshot and
    replace the object reference under a lock, so concurrent readers see
    either the previous complete snapshot or the next complete snapshot.
    Cross-worker cache coherence is provided by Redis pub/sub invalidation
    via ``publish_feature_flags_invalidate()``.
    """

    def __init__(
        self,
        loader: FeatureFlagLoader | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._loader = loader or (lambda: ())
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._snapshot: FeatureFlagRegistrySnapshot | None = None

    def _coerce_record(
        self,
        raw: FeatureFlagRecord | Mapping[str, Any],
    ) -> FeatureFlagRecord:
        if isinstance(raw, FeatureFlagRecord):
            return raw
        return FeatureFlagRecord(
            flag_name=str(raw["flag_name"]).strip(),
            tier=FeatureFlagTier.parse(raw["tier"]),
            state=FeatureFlagState.parse(raw["state"]),
            expires_at=(
                None
                if raw.get("expires_at") is None
                else str(raw.get("expires_at"))
            ),
            owner=str(raw.get("owner") or ""),
            created_at=str(raw.get("created_at") or ""),
        )

    def _load_snapshot_unlocked(self) -> FeatureFlagRegistrySnapshot:
        rows = {}
        for raw in self._loader():
            record = self._coerce_record(raw)
            if not record.flag_name:
                continue
            rows[record.flag_name] = record
        snapshot = FeatureFlagRegistrySnapshot(
            flags=MappingProxyType(rows),
            loaded_at=self._clock(),
        )
        self._snapshot = snapshot
        return snapshot

    def snapshot(self) -> FeatureFlagRegistrySnapshot:
        """Return the current immutable snapshot, loading it on miss."""
        snapshot = self._snapshot
        if snapshot is not None:
            return snapshot
        with self._lock:
            snapshot = self._snapshot
            if snapshot is not None:
                return snapshot
            return self._load_snapshot_unlocked()

    def reload(self) -> FeatureFlagRegistrySnapshot:
        """Force a fresh snapshot and replace the cache atomically."""
        with self._lock:
            return self._load_snapshot_unlocked()

    def invalidate(self) -> None:
        """Clear the local snapshot; next read reloads through ``loader``."""
        with self._lock:
            self._snapshot = None

    def get_record(self, flag_name: str) -> FeatureFlagRecord | None:
        """Return a cached record by flag name without per-read DB work."""
        key = str(flag_name).strip()
        if not key:
            return None
        return self.snapshot().flags.get(key)

    def get_global_state(
        self,
        flag_name: str,
        *,
        default: str | FeatureFlagState | None = None,
    ) -> FeatureFlagState | None:
        """Return cached global state or ``default`` when the row is absent."""
        record = self.get_record(flag_name)
        if record is not None:
            return record.state
        if default is None:
            return None
        return FeatureFlagState.parse(default)


FEATURE_FLAGS_INVALIDATE_EVENT = "feature_flags_invalidate"


default_feature_flag_registry = FeatureFlagRegistry()


def get_feature_flag_global_state(
    flag_name: str,
    *,
    default: str | FeatureFlagState | None = None,
    registry: FeatureFlagRegistry | None = None,
) -> FeatureFlagState | None:
    """Hot-path helper for reading the cached global state of a flag."""
    return (registry or default_feature_flag_registry).get_global_state(
        flag_name,
        default=default,
    )


def invalidate_feature_flags_cache() -> None:
    """Clear this worker's default feature-flag snapshot."""
    default_feature_flag_registry.invalidate()


def publish_feature_flags_invalidate(
    *,
    flag_name: str | None = None,
    origin_worker: str | None = None,
) -> bool:
    """Invalidate local cache and fan out a Redis pub/sub signal.

    Returns the underlying ``publish_cross_worker`` result so callers can
    surface degraded local-only invalidation when Redis is not configured.
    """
    invalidate_feature_flags_cache()
    payload = {
        "flag_name": flag_name or "",
        "origin_worker": origin_worker or "",
    }
    try:
        from backend.shared_state import publish_cross_worker
        return publish_cross_worker(FEATURE_FLAGS_INVALIDATE_EVENT, payload)
    except Exception as exc:  # pragma: no cover - defensive bootstrap guard
        logger.debug("feature_flags invalidate publish failed: %s", exc)
        return False


def _on_feature_flags_invalidate_event(event: str, data: dict) -> None:
    """Cross-worker callback: clear this worker's feature-flag snapshot."""
    if event != FEATURE_FLAGS_INVALIDATE_EVENT:
        return
    invalidate_feature_flags_cache()
    origin = (
        data.get("origin_worker", "<unknown>")
        if isinstance(data, dict)
        else "<unknown>"
    )
    flag_name = data.get("flag_name", "") if isinstance(data, dict) else ""
    logger.info(
        "feature_flags cross-worker invalidate received (origin=%s flag=%s)",
        origin,
        flag_name,
    )


try:
    from backend.shared_state import register_cross_worker_callback as _register_cb
    _register_cb(_on_feature_flags_invalidate_event)
except Exception:  # pragma: no cover - defensive, like pricing.py
    pass


__all__ = [
    "FEATURE_FLAGS_INVALIDATE_EVENT",
    "FEATURE_FLAG_ENV_FALSE_VALUES",
    "FEATURE_FLAG_ENV_KNOBS",
    "FEATURE_FLAG_ENV_PREFIXES",
    "FEATURE_FLAG_ENV_TRUE_VALUES",
    "FEATURE_FLAG_RESOLUTION_SOURCE_ORDER",
    "FEATURE_FLAG_STATE_VALUES",
    "FEATURE_FLAG_STATE_VALUE_SET",
    "FEATURE_FLAG_TIER_DEFINITIONS",
    "FEATURE_FLAG_TIER_ORDER",
    "FEATURE_FLAG_TIER_VALUES",
    "FEATURE_FLAG_TIER_VALUE_SET",
    "FeatureFlagResolution",
    "FeatureFlagResolutionSource",
    "FeatureFlagExpiryViolation",
    "FeatureFlagEnvKnob",
    "FeatureFlagRegistry",
    "FeatureFlagRegistrySnapshot",
    "FeatureFlagRecord",
    "FeatureFlagState",
    "FeatureFlagTier",
    "FeatureFlagTierDefinition",
    "assert_no_expired_feature_flags",
    "default_feature_flag_registry",
    "feature_flag_name_for_env_knob",
    "find_expired_feature_flags",
    "get_feature_flag_global_state",
    "invalidate_feature_flags_cache",
    "is_feature_flag_env_knob",
    "is_feature_flag_state",
    "is_feature_flag_tier",
    "publish_feature_flags_invalidate",
    "resolve_env_backed_feature_flag",
    "resolve_feature_flag_state",
]

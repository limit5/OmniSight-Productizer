"""OP-773 D12 (Sprint D) — feature flag SDK with rollout + segmentation.

This module is the public entry point for application code that wants to
ask ``is_flag_enabled(name, user_id=None, segment=None)`` without caring
about the DB row, the legacy env var fallback, the rollout percentage,
or the segment filter.  It composes three orthogonal layers:

1. **Global enabled / disabled** — read from the WP.7.1 ``feature_flags``
   table via :mod:`backend.feature_flags`.  Falls back to the legacy
   ``OMNISIGHT_*_ENABLED`` env var (and finally the rollout config's
   ``default_enabled``) when no row is registered yet.

2. **Percent rollout** — :func:`bucket_for` returns a stable integer in
   ``[0, 99]`` from ``sha1(flag_name + ":" + user_id)``.  When the DB
   row is enabled and the bucket is below ``rollout_pct``, the SDK
   returns ``True``.  When ``user_id`` is ``None``, the SDK returns
   ``True`` only at ``rollout_pct == 100`` so anonymous traffic gets
   the conservative answer during partial rollouts.

3. **Segment filter** — optional dict of ``{key: value}`` equality
   checks the caller's ``segment`` mapping must satisfy.  An empty or
   missing filter passes; a missing segment key fails closed.

Cache contract
--------------
Reads are served from a process-local in-memory cache with a **30 s
TTL** (OP-773 AC #5).  After the operator flips a flag in the
``/admin/feature-flags`` UI, every backend worker reflects the new
value within ``30 s`` even without a Redis pub/sub fan-out, because
each worker's TTL expires independently.  Workers that ARE wired to
the WP.7.4 Redis fan-out invalidate sooner via
:func:`backend.feature_flags.invalidate_feature_flags_cache`; the SDK's
TTL is the worst-case ceiling.

Boundary note (OP-773 §11)
--------------------------
This SDK is the backend slice of OP-773.  The DB-side schema additions
(``rollout_pct`` + ``segment_filter`` columns + ``updated_at``) and the
``/admin/feature-flags`` UI extension to expose those controls are
``area:db`` and ``area:frontend`` work and are tracked as the OP-773
follow-up.  Until those land, rollout / segment metadata lives in
``config/feature_flag_rollouts.yaml`` (declarative, devops-owned) and
is hot-reloaded on the same 30 s TTL.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from backend import feature_flags as _flags


logger = logging.getLogger(__name__)


CACHE_TTL_SECONDS: float = 30.0


_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROLLOUT_CONFIG_PATH = _REPO_ROOT / "config" / "feature_flag_rollouts.yaml"


@dataclass(frozen=True)
class FeatureFlagRollout:
    """One flag's rollout layer (rollout_pct + segment_filter + alias)."""

    name: str
    rollout_pct: int
    env_alias: str | None
    default_enabled: bool
    segment_filter: Mapping[str, Any]
    owner: str

    def __post_init__(self) -> None:
        if not 0 <= self.rollout_pct <= 100:
            raise ValueError(
                f"rollout_pct must be in [0,100]; got {self.rollout_pct} "
                f"for flag {self.name!r}"
            )


def _coerce_rollout(raw: Mapping[str, Any]) -> FeatureFlagRollout:
    name = str(raw["name"]).strip()
    if not name:
        raise ValueError("rollout entry missing 'name'")
    return FeatureFlagRollout(
        name=name,
        rollout_pct=int(raw.get("rollout_pct", 100)),
        env_alias=(
            str(raw["env_alias"]).strip()
            if raw.get("env_alias")
            else None
        ),
        default_enabled=bool(raw.get("default_enabled", True)),
        segment_filter=dict(raw.get("segment_filter") or {}),
        owner=str(raw.get("owner") or ""),
    )


def load_rollout_config(
    path: str | os.PathLike[str] | None = None,
) -> dict[str, FeatureFlagRollout]:
    """Parse ``config/feature_flag_rollouts.yaml`` into a name→rollout map."""
    target = Path(path) if path is not None else DEFAULT_ROLLOUT_CONFIG_PATH
    if not target.exists():
        return {}
    payload = yaml.safe_load(target.read_text()) or {}
    flags = payload.get("flags") or []
    out: dict[str, FeatureFlagRollout] = {}
    for raw in flags:
        rollout = _coerce_rollout(raw)
        out[rollout.name] = rollout
    return out


def bucket_for(flag_name: str, user_id: str | int | None) -> int:
    """Return a stable bucket in ``[0, 99]`` for ``(flag_name, user_id)``.

    The hash is SHA1 over ``"<flag>:<user_id>"`` then ``% 100``.  SHA1
    is used for distribution quality only — never for security.  The
    bucket function is pure and deterministic across workers and
    Python versions, so different replicas reach the same verdict.
    """
    if user_id is None:
        token = f"{flag_name}:__anonymous__"
    else:
        token = f"{flag_name}:{user_id}"
    digest = hashlib.sha1(token.encode("utf-8")).digest()
    bucket_seed = int.from_bytes(digest[:4], "big")
    return bucket_seed % 100


def _segment_matches(
    segment: Mapping[str, Any] | None,
    segment_filter: Mapping[str, Any],
) -> bool:
    if not segment_filter:
        return True
    if not segment:
        return False
    for key, expected in segment_filter.items():
        actual = segment.get(key)
        if actual != expected:
            return False
    return True


def _resolve_global_enabled(
    rollout: FeatureFlagRollout,
    *,
    registry: _flags.FeatureFlagRegistry | None = None,
) -> bool:
    """Layer 1: DB row → env alias → default_enabled."""
    state = _flags.get_feature_flag_global_state(rollout.name, registry=registry)
    if state is _flags.FeatureFlagState.ENABLED:
        return True
    if state is _flags.FeatureFlagState.DISABLED:
        return False
    if rollout.env_alias:
        raw = os.environ.get(rollout.env_alias)
        if raw is not None:
            value = raw.strip().lower()
            if value in _flags.FEATURE_FLAG_ENV_TRUE_VALUES:
                return True
            if value in _flags.FEATURE_FLAG_ENV_FALSE_VALUES:
                return False
    return rollout.default_enabled


@dataclass
class _CacheEntry:
    value: bool
    expires_at: float


class FeatureFlagSDK:
    """Process-local SDK with 30 s TTL cache + rollout + segmentation.

    The SDK is intentionally a small object held as a module-global
    singleton (:data:`default_sdk`) so that import-time wiring stays
    cheap and tests can stand up isolated instances with their own
    config and clock.
    """

    def __init__(
        self,
        *,
        rollout_config_path: str | os.PathLike[str] | None = None,
        registry: _flags.FeatureFlagRegistry | None = None,
        cache_ttl_seconds: float = CACHE_TTL_SECONDS,
        clock: Any = None,
    ) -> None:
        self._config_path = (
            Path(rollout_config_path)
            if rollout_config_path is not None
            else DEFAULT_ROLLOUT_CONFIG_PATH
        )
        self._registry = registry
        self._cache_ttl = float(cache_ttl_seconds)
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._cache: dict[tuple[str, str, str], _CacheEntry] = {}
        self._rollouts: dict[str, FeatureFlagRollout] = {}
        self._rollouts_loaded_at: float = 0.0

    def _ensure_rollouts_fresh(self) -> dict[str, FeatureFlagRollout]:
        now = self._clock()
        if (
            self._rollouts_loaded_at != 0.0
            and (now - self._rollouts_loaded_at) < self._cache_ttl
        ):
            return self._rollouts
        with self._lock:
            now = self._clock()
            if (
                self._rollouts_loaded_at != 0.0
                and (now - self._rollouts_loaded_at) < self._cache_ttl
            ):
                return self._rollouts
            self._rollouts = load_rollout_config(self._config_path)
            self._rollouts_loaded_at = now
            return self._rollouts

    def list_rollouts(self) -> list[FeatureFlagRollout]:
        """Return the current rollout entries (used by the admin UI)."""
        return list(self._ensure_rollouts_fresh().values())

    def _segment_cache_key(
        self,
        segment: Mapping[str, Any] | None,
    ) -> str:
        if not segment:
            return ""
        return ";".join(f"{k}={segment[k]}" for k in sorted(segment))

    def is_flag_enabled(
        self,
        name: str,
        user_id: str | int | None = None,
        segment: Mapping[str, Any] | None = None,
    ) -> bool:
        """Resolve a flag for one ``(user_id, segment)`` pair.

        Cached per ``(name, user_id, segment)`` for ``CACHE_TTL_SECONDS``.
        """
        flag_name = str(name).strip()
        if not flag_name:
            raise ValueError("flag name must be a non-empty string")
        user_key = "" if user_id is None else str(user_id)
        seg_key = self._segment_cache_key(segment)
        cache_key = (flag_name, user_key, seg_key)

        now = self._clock()
        cached = self._cache.get(cache_key)
        if cached is not None and cached.expires_at > now:
            return cached.value

        value = self._compute(flag_name, user_id, segment)
        self._cache[cache_key] = _CacheEntry(
            value=value,
            expires_at=now + self._cache_ttl,
        )
        return value

    def _compute(
        self,
        flag_name: str,
        user_id: str | int | None,
        segment: Mapping[str, Any] | None,
    ) -> bool:
        rollouts = self._ensure_rollouts_fresh()
        rollout = rollouts.get(flag_name)
        if rollout is None:
            state = _flags.get_feature_flag_global_state(
                flag_name,
                registry=self._registry,
            )
            if state is _flags.FeatureFlagState.ENABLED:
                return True
            return False

        if not _resolve_global_enabled(rollout, registry=self._registry):
            return False

        if not _segment_matches(segment, rollout.segment_filter):
            return False

        if rollout.rollout_pct >= 100:
            return True
        if rollout.rollout_pct <= 0:
            return False
        if user_id is None:
            return False
        return bucket_for(flag_name, user_id) < rollout.rollout_pct

    def invalidate(self, name: str | None = None) -> None:
        """Clear cached resolutions.  ``name=None`` clears every entry."""
        with self._lock:
            if name is None:
                self._cache.clear()
                self._rollouts_loaded_at = 0.0
                return
            keys = [k for k in self._cache if k[0] == name]
            for k in keys:
                self._cache.pop(k, None)


default_sdk = FeatureFlagSDK()


def is_flag_enabled(
    name: str,
    user_id: str | int | None = None,
    segment: Mapping[str, Any] | None = None,
) -> bool:
    """Module-level convenience wrapper around :data:`default_sdk`."""
    return default_sdk.is_flag_enabled(name, user_id=user_id, segment=segment)


def invalidate_flag_sdk_cache(name: str | None = None) -> None:
    """Module-level convenience wrapper for :meth:`FeatureFlagSDK.invalidate`."""
    default_sdk.invalidate(name=name)


__all__ = [
    "CACHE_TTL_SECONDS",
    "DEFAULT_ROLLOUT_CONFIG_PATH",
    "FeatureFlagRollout",
    "FeatureFlagSDK",
    "bucket_for",
    "default_sdk",
    "invalidate_flag_sdk_cache",
    "is_flag_enabled",
    "load_rollout_config",
]

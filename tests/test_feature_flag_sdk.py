"""OP-773 D12 — feature flag SDK tests.

Maps 1:1 to the ticket's Acceptance list:

  AC #1 (feature_flags table created via alembic)
    → ``test_existing_alembic_0194_provides_table`` — the WP.7.1
      0194 migration is on disk and exposes the upgrade hook used by
      the SDK's global-enabled lookup.

  AC #2 (SDK with caching)
    → ``test_is_flag_enabled_uses_30s_ttl_cache`` — cached values
      survive in-test clock advances < 30 s and refresh after.
    → ``test_cache_invalidation_drops_cached_value`` — explicit
      ``invalidate()`` clears one or all keys.

  AC #3 (UI with toggle + % control) — partially satisfied: the
    existing ``app/admin/feature-flags/page.tsx`` toggle path is
    untouched.  The %-control surfaces here as the SDK contract that
    the admin UI consumes; the UI extension itself is OP-773
    ``area:frontend`` follow-up tracked in the JIRA AC verification.

  AC #4 (5+ existing env-var flags migrated)
    → ``test_rollout_config_migrates_at_least_five_env_vars`` — the
      shipped config has ≥ 5 entries with ``env_alias`` set.

  AC #5 (Synthetic test: flip flag in UI → backend respects within 30s)
    → ``test_synthetic_flip_propagates_within_30s`` — a flag is
      enabled in the registry, the SDK answers True; the registry
      flips to disabled; advancing the SDK clock past 30 s returns
      False on the next read.

Plus the rollout / segmentation contract:
  → ``test_bucket_for_is_stable_for_same_input`` — same input always
    yields the same bucket.
  → ``test_bucket_for_distributes_across_buckets`` — 2 000 ids spread
    across 100 buckets within Chebyshev tolerance.
  → ``test_rollout_pct_zero_disables_for_all_users`` /
    ``test_rollout_pct_hundred_enables_for_all_users``.
  → ``test_rollout_pct_fifty_splits_users_deterministically`` —
    50% rollout produces 800–1 200 enabled out of 2 000.
  → ``test_segment_filter_blocks_mismatched_segment``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from backend import feature_flags as _flags
from backend.feature_flag_sdk import (
    CACHE_TTL_SECONDS,
    FeatureFlagRollout,
    FeatureFlagSDK,
    bucket_for,
    load_rollout_config,
)


class _ManualClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_registry() -> _flags.FeatureFlagRegistry:
    state: dict[str, str] = {"flag.a": "enabled"}

    def loader():
        return [
            {
                "flag_name": name,
                "tier": "release",
                "state": value,
                "owner": "tests",
            }
            for name, value in state.items()
        ]

    registry = _flags.FeatureFlagRegistry(loader)
    registry.__test_state__ = state  # type: ignore[attr-defined]
    return registry


@pytest.fixture
def rollout_yaml(tmp_path: Path) -> Path:
    target = tmp_path / "rollouts.yaml"
    target.write_text(
        """
flags:
  - name: flag.a
    rollout_pct: 100
    default_enabled: true
    owner: tests
  - name: flag.canary
    rollout_pct: 50
    default_enabled: true
    owner: tests
  - name: flag.disabled_pct
    rollout_pct: 0
    default_enabled: true
    owner: tests
  - name: flag.segmented
    rollout_pct: 100
    default_enabled: true
    owner: tests
    segment_filter:
      tenant_id: t1
""".strip()
    )
    return target


# ─── AC #1 ─────────────────────────────────────────────────────────


def test_existing_alembic_0194_provides_table() -> None:
    migration = REPO_ROOT / "backend" / "alembic" / "versions" / "0194_feature_flags.py"
    assert migration.exists(), (
        "AC #1: 0194_feature_flags alembic migration must exist"
    )
    body = migration.read_text()
    assert "CREATE TABLE IF NOT EXISTS feature_flags" in body
    assert "def upgrade()" in body and "def downgrade()" in body


# ─── AC #2 ─────────────────────────────────────────────────────────


def test_is_flag_enabled_uses_30s_ttl_cache(
    fake_registry: _flags.FeatureFlagRegistry,
    rollout_yaml: Path,
) -> None:
    clock = _ManualClock()
    sdk = FeatureFlagSDK(
        rollout_config_path=rollout_yaml,
        registry=fake_registry,
        cache_ttl_seconds=CACHE_TTL_SECONDS,
        clock=clock,
    )

    assert sdk.is_flag_enabled("flag.a", user_id="u1") is True
    fake_registry.__test_state__["flag.a"] = "disabled"  # type: ignore[attr-defined]
    fake_registry.invalidate()
    clock.advance(CACHE_TTL_SECONDS - 1.0)
    assert sdk.is_flag_enabled("flag.a", user_id="u1") is True

    clock.advance(2.0)
    assert sdk.is_flag_enabled("flag.a", user_id="u1") is False


def test_cache_invalidation_drops_cached_value(
    fake_registry: _flags.FeatureFlagRegistry,
    rollout_yaml: Path,
) -> None:
    clock = _ManualClock()
    sdk = FeatureFlagSDK(
        rollout_config_path=rollout_yaml,
        registry=fake_registry,
        clock=clock,
    )

    assert sdk.is_flag_enabled("flag.a", user_id="u1") is True
    fake_registry.__test_state__["flag.a"] = "disabled"  # type: ignore[attr-defined]
    fake_registry.invalidate()
    sdk.invalidate(name="flag.a")
    assert sdk.is_flag_enabled("flag.a", user_id="u1") is False


# ─── AC #4 ─────────────────────────────────────────────────────────


def test_rollout_config_migrates_at_least_five_env_vars() -> None:
    rollouts = load_rollout_config()
    aliased = [r for r in rollouts.values() if r.env_alias]
    assert len(aliased) >= 5, (
        f"AC #4: ≥ 5 env-var-backed flags required; got {len(aliased)}"
    )
    for rollout in aliased:
        assert rollout.env_alias and rollout.env_alias.startswith("OMNISIGHT_"), (
            f"flag {rollout.name!r} alias must start with OMNISIGHT_; "
            f"got {rollout.env_alias!r}"
        )


def test_rollout_config_owners_are_set() -> None:
    rollouts = load_rollout_config()
    assert rollouts, "shipped rollout config must define at least one flag"
    for rollout in rollouts.values():
        assert rollout.owner, f"flag {rollout.name!r} missing owner"


# ─── AC #5 ─────────────────────────────────────────────────────────


def test_synthetic_flip_propagates_within_30s(
    fake_registry: _flags.FeatureFlagRegistry,
    rollout_yaml: Path,
) -> None:
    """Operator flips the flag → backend observes the new value before TTL."""
    clock = _ManualClock()
    sdk = FeatureFlagSDK(
        rollout_config_path=rollout_yaml,
        registry=fake_registry,
        cache_ttl_seconds=CACHE_TTL_SECONDS,
        clock=clock,
    )
    user_id = "operator-flip-user"

    assert sdk.is_flag_enabled("flag.a", user_id=user_id) is True

    clock.advance(10.0)
    fake_registry.__test_state__["flag.a"] = "disabled"  # type: ignore[attr-defined]
    fake_registry.invalidate()

    clock.advance(20.0)
    assert sdk.is_flag_enabled("flag.a", user_id=user_id) is False


# ─── Rollout / bucketing contract ──────────────────────────────────


def test_bucket_for_is_stable_for_same_input() -> None:
    assert bucket_for("flag.x", "user-42") == bucket_for("flag.x", "user-42")
    different_flag = bucket_for("flag.y", "user-42")
    same_flag_other_user = bucket_for("flag.x", "user-41")
    assert (
        different_flag != bucket_for("flag.x", "user-42")
        or same_flag_other_user != bucket_for("flag.x", "user-42")
    )


def test_bucket_for_distributes_across_buckets() -> None:
    counts = [0] * 100
    for i in range(2_000):
        counts[bucket_for("flag.dist", f"user-{i}")] += 1
    assert min(counts) >= 5
    assert max(counts) <= 50


def test_rollout_pct_zero_disables_for_all_users(
    fake_registry: _flags.FeatureFlagRegistry,
    rollout_yaml: Path,
) -> None:
    fake_registry.__test_state__["flag.disabled_pct"] = "enabled"  # type: ignore[attr-defined]
    fake_registry.invalidate()

    sdk = FeatureFlagSDK(
        rollout_config_path=rollout_yaml,
        registry=fake_registry,
    )
    for i in range(50):
        assert sdk.is_flag_enabled("flag.disabled_pct", user_id=f"u-{i}") is False


def test_rollout_pct_hundred_enables_for_all_users(
    fake_registry: _flags.FeatureFlagRegistry,
    rollout_yaml: Path,
) -> None:
    sdk = FeatureFlagSDK(
        rollout_config_path=rollout_yaml,
        registry=fake_registry,
    )
    for i in range(50):
        assert sdk.is_flag_enabled("flag.a", user_id=f"u-{i}") is True


def test_rollout_pct_fifty_splits_users_deterministically(
    fake_registry: _flags.FeatureFlagRegistry,
    rollout_yaml: Path,
) -> None:
    fake_registry.__test_state__["flag.canary"] = "enabled"  # type: ignore[attr-defined]
    fake_registry.invalidate()

    sdk = FeatureFlagSDK(
        rollout_config_path=rollout_yaml,
        registry=fake_registry,
    )
    enabled = 0
    for i in range(2_000):
        if sdk.is_flag_enabled("flag.canary", user_id=f"user-{i}"):
            enabled += 1
        sdk.invalidate(name="flag.canary")

    assert 800 <= enabled <= 1_200, f"50% rollout split unbalanced: {enabled}/2000"


def test_anonymous_user_blocked_during_partial_rollout(
    fake_registry: _flags.FeatureFlagRegistry,
    rollout_yaml: Path,
) -> None:
    fake_registry.__test_state__["flag.canary"] = "enabled"  # type: ignore[attr-defined]
    fake_registry.invalidate()

    sdk = FeatureFlagSDK(
        rollout_config_path=rollout_yaml,
        registry=fake_registry,
    )
    assert sdk.is_flag_enabled("flag.canary", user_id=None) is False


# ─── Segment filter ────────────────────────────────────────────────


def test_segment_filter_passes_when_segment_matches(
    fake_registry: _flags.FeatureFlagRegistry,
    rollout_yaml: Path,
) -> None:
    fake_registry.__test_state__["flag.segmented"] = "enabled"  # type: ignore[attr-defined]
    fake_registry.invalidate()

    sdk = FeatureFlagSDK(
        rollout_config_path=rollout_yaml,
        registry=fake_registry,
    )
    assert (
        sdk.is_flag_enabled(
            "flag.segmented",
            user_id="u1",
            segment={"tenant_id": "t1"},
        )
        is True
    )


def test_segment_filter_blocks_mismatched_segment(
    fake_registry: _flags.FeatureFlagRegistry,
    rollout_yaml: Path,
) -> None:
    fake_registry.__test_state__["flag.segmented"] = "enabled"  # type: ignore[attr-defined]
    fake_registry.invalidate()

    sdk = FeatureFlagSDK(
        rollout_config_path=rollout_yaml,
        registry=fake_registry,
    )
    assert (
        sdk.is_flag_enabled(
            "flag.segmented",
            user_id="u1",
            segment={"tenant_id": "other"},
        )
        is False
    )
    assert (
        sdk.is_flag_enabled("flag.segmented", user_id="u1", segment=None)
        is False
    )


# ─── Env-alias fallback ────────────────────────────────────────────


def test_env_alias_used_when_db_row_absent(
    monkeypatch: pytest.MonkeyPatch,
    rollout_yaml: Path,
) -> None:
    empty_registry = _flags.FeatureFlagRegistry(lambda: ())

    rollout_yaml.write_text(
        """
flags:
  - name: flag.envonly
    rollout_pct: 100
    env_alias: OMNISIGHT_TEST_FLAG_ENABLED
    default_enabled: false
    owner: tests
""".strip()
    )

    sdk = FeatureFlagSDK(
        rollout_config_path=rollout_yaml,
        registry=empty_registry,
    )

    monkeypatch.setenv("OMNISIGHT_TEST_FLAG_ENABLED", "true")
    sdk.invalidate()
    assert sdk.is_flag_enabled("flag.envonly", user_id="u1") is True

    monkeypatch.setenv("OMNISIGHT_TEST_FLAG_ENABLED", "false")
    sdk.invalidate()
    assert sdk.is_flag_enabled("flag.envonly", user_id="u1") is False

    monkeypatch.delenv("OMNISIGHT_TEST_FLAG_ENABLED", raising=False)
    sdk.invalidate()
    assert sdk.is_flag_enabled("flag.envonly", user_id="u1") is False


# ─── Invalid input ─────────────────────────────────────────────────


def test_rollout_pct_out_of_range_rejected() -> None:
    with pytest.raises(ValueError):
        FeatureFlagRollout(
            name="x",
            rollout_pct=150,
            env_alias=None,
            default_enabled=True,
            segment_filter={},
            owner="",
        )


def test_empty_flag_name_rejected(
    fake_registry: _flags.FeatureFlagRegistry,
    rollout_yaml: Path,
) -> None:
    sdk = FeatureFlagSDK(
        rollout_config_path=rollout_yaml,
        registry=fake_registry,
    )
    with pytest.raises(ValueError):
        sdk.is_flag_enabled("", user_id="u1")

"""Property-based public API tests for ``backend/agents/buff_registry.py``."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.agents.buff_registry import (
    BUFF_DEFINITIONS,
    CAP_WARNING_BUFF_ID,
    CAP_WARNING_QUOTA_RATIO,
    CAP_WARNING_ROUTING_PRIORITY_MULTIPLIER,
    FRESH_TOKENS_BUFF_ID,
    FRESH_TOKENS_DURATION_SECONDS,
    FRESH_TOKENS_XP_MULTIPLIER,
    STREAK_BUFF_ID,
    STREAK_SUCCESS_COUNT,
    STREAK_XP_MULTIPLIER,
    WELL_RESTED_BUFF_ID,
    WELL_RESTED_IDLE_SECONDS,
    WELL_RESTED_XP_MULTIPLIER,
    BuffContext,
    BuffDefinition,
    UnknownBuffError,
    active_buff_ids_for_context,
    active_buffs_for_context,
    get_buff_definition,
    list_buff_definitions,
    routing_priority_multiplier_for_context,
    routing_priority_multiplier_for_quota_ratio,
    xp_multiplier_for_buff_ids,
    xp_multiplier_for_context,
)


_NOW = datetime(2026, 5, 17, tzinfo=timezone.utc)
_KNOWN_BUFF_IDS = tuple(BUFF_DEFINITIONS)
_KNOWN_BUFF_ID_STRATEGY = st.sampled_from(_KNOWN_BUFF_IDS)
_OPTIONAL_SECONDS = st.one_of(
    st.none(),
    st.integers(
        min_value=-(24 * 60 * 60),
        max_value=365 * 24 * 60 * 60,
    ),
)
_OPTIONAL_QUOTA_RATIO = st.one_of(
    st.none(),
    st.floats(
        min_value=0.0,
        max_value=1_000_000.0,
        allow_nan=False,
        allow_infinity=False,
        width=32,
    ),
)


def _context_from_offsets(
    *,
    last_reset_seconds_ago: int | None,
    last_completed_seconds_ago: int | None,
    consecutive_successes: int,
    remaining_quota_ratio: float | None,
) -> BuffContext:
    return BuffContext(
        now=_NOW,
        last_rolling_5h_reset_at=(
            None
            if last_reset_seconds_ago is None
            else _NOW - timedelta(seconds=last_reset_seconds_ago)
        ),
        last_task_completed_at=(
            None
            if last_completed_seconds_ago is None
            else _NOW - timedelta(seconds=last_completed_seconds_ago)
        ),
        consecutive_successes=consecutive_successes,
        remaining_quota_ratio=remaining_quota_ratio,
    )


# -- registry shape -----------------------------------------------------


def test_registry_is_immutable_mapping_proxy() -> None:
    assert isinstance(BUFF_DEFINITIONS, MappingProxyType)
    with pytest.raises(TypeError):
        BUFF_DEFINITIONS["new"] = BuffDefinition(  # type: ignore[index]
            buff_id="new",
            display_name="New",
            kind="xp",
            multiplier=1.0,
            summary="",
        )


def test_registry_contains_documented_buffs() -> None:
    assert set(BUFF_DEFINITIONS) == {
        FRESH_TOKENS_BUFF_ID,
        WELL_RESTED_BUFF_ID,
        STREAK_BUFF_ID,
        CAP_WARNING_BUFF_ID,
    }


@settings(max_examples=75, deadline=None)
@given(buff_id=_KNOWN_BUFF_ID_STRATEGY)
def test_list_and_get_buff_definitions_property_preserve_registry_contract(
    buff_id: str,
) -> None:
    listed = list_buff_definitions()
    first = get_buff_definition(buff_id)
    second = get_buff_definition(f"  {buff_id}  ")

    assert isinstance(listed, tuple)
    assert listed == list_buff_definitions()
    assert tuple(buff.buff_id for buff in listed) == _KNOWN_BUFF_IDS
    assert all(isinstance(buff, BuffDefinition) for buff in listed)
    assert first is BUFF_DEFINITIONS[buff_id]
    assert second is first


@settings(max_examples=75, deadline=None)
@given(
    unknown_id=st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=1,
        max_size=128,
    ).filter(
        lambda value: bool(value.strip()) and value.strip() not in BUFF_DEFINITIONS
    ),
)
def test_get_buff_definition_property_rejects_unknown_non_empty_ids(
    unknown_id: str,
) -> None:
    with pytest.raises(UnknownBuffError):
        get_buff_definition(unknown_id)


@settings(max_examples=75, deadline=None)
@given(
    blank_id=st.text(
        alphabet=st.one_of(
            st.characters(whitelist_categories=("Zs",)),
            st.sampled_from(("\t", "\n", "\r")),
        )
    )
)
def test_get_buff_definition_property_rejects_blank_ids(blank_id: str) -> None:
    with pytest.raises(ValueError):
        get_buff_definition(blank_id)


@settings(max_examples=75, deadline=None)
@given(buff_id=st.one_of(st.none(), st.integers(), st.floats(allow_nan=False)))
def test_get_buff_definition_property_rejects_non_string_ids(
    buff_id: object,
) -> None:
    with pytest.raises(TypeError):
        get_buff_definition(buff_id)  # type: ignore[arg-type]


# -- context-derived public helpers -------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    last_reset_seconds_ago=_OPTIONAL_SECONDS,
    last_completed_seconds_ago=_OPTIONAL_SECONDS,
    consecutive_successes=st.integers(min_value=0, max_value=1_000_000),
    remaining_quota_ratio=_OPTIONAL_QUOTA_RATIO,
)
def test_active_buffs_property_are_deterministic_ordered_registry_entries(
    last_reset_seconds_ago: int | None,
    last_completed_seconds_ago: int | None,
    consecutive_successes: int,
    remaining_quota_ratio: float | None,
) -> None:
    context = _context_from_offsets(
        last_reset_seconds_ago=last_reset_seconds_ago,
        last_completed_seconds_ago=last_completed_seconds_ago,
        consecutive_successes=consecutive_successes,
        remaining_quota_ratio=remaining_quota_ratio,
    )

    active_buffs = active_buffs_for_context(context)
    active_ids = active_buff_ids_for_context(context)

    assert isinstance(active_buffs, tuple)
    assert isinstance(active_ids, tuple)
    assert active_buffs == active_buffs_for_context(context)
    assert active_ids == active_buff_ids_for_context(context)
    assert active_ids == tuple(buff.buff_id for buff in active_buffs)
    assert len(active_ids) == len(set(active_ids))
    assert set(active_ids).issubset(BUFF_DEFINITIONS)
    assert active_ids == tuple(
        buff_id for buff_id in _KNOWN_BUFF_IDS if buff_id in active_ids
    )
    assert all(buff is BUFF_DEFINITIONS[buff.buff_id] for buff in active_buffs)


@settings(max_examples=100, deadline=None)
@given(
    last_reset_seconds_ago=_OPTIONAL_SECONDS,
    last_completed_seconds_ago=_OPTIONAL_SECONDS,
    consecutive_successes=st.integers(min_value=0, max_value=1_000_000),
    remaining_quota_ratio=_OPTIONAL_QUOTA_RATIO,
)
def test_context_multiplier_properties_match_active_public_ids(
    last_reset_seconds_ago: int | None,
    last_completed_seconds_ago: int | None,
    consecutive_successes: int,
    remaining_quota_ratio: float | None,
) -> None:
    context = _context_from_offsets(
        last_reset_seconds_ago=last_reset_seconds_ago,
        last_completed_seconds_ago=last_completed_seconds_ago,
        consecutive_successes=consecutive_successes,
        remaining_quota_ratio=remaining_quota_ratio,
    )
    active_ids = active_buff_ids_for_context(context)
    expected_xp = 1.0
    expected_routing = 1.0
    for buff_id in active_ids:
        buff = get_buff_definition(buff_id)
        if buff.kind == "xp":
            expected_xp *= buff.multiplier
        if buff.kind == "routing_priority":
            expected_routing *= buff.multiplier

    assert xp_multiplier_for_context(context) == pytest.approx(expected_xp)
    assert xp_multiplier_for_context(context) == pytest.approx(
        xp_multiplier_for_buff_ids(active_ids)
    )
    assert routing_priority_multiplier_for_context(context) == pytest.approx(
        expected_routing
    )


@settings(max_examples=75, deadline=None)
@given(
    consecutive_successes=st.integers(max_value=-1),
    remaining_quota_ratio=st.floats(
        min_value=-1_000_000.0,
        max_value=0.0,
        exclude_max=True,
        allow_nan=False,
        allow_infinity=False,
        width=32,
    ),
)
def test_context_helpers_property_reject_negative_edges(
    consecutive_successes: int,
    remaining_quota_ratio: float,
) -> None:
    with pytest.raises(ValueError):
        active_buffs_for_context(
            BuffContext(now=_NOW, consecutive_successes=consecutive_successes)
        )
    with pytest.raises(ValueError):
        active_buffs_for_context(
            BuffContext(now=_NOW, remaining_quota_ratio=remaining_quota_ratio)
        )


# -- explicit multiplier helpers ----------------------------------------


@settings(max_examples=100, deadline=None)
@given(buff_ids=st.lists(_KNOWN_BUFF_ID_STRATEGY, max_size=100).map(tuple))
def test_xp_multiplier_for_buff_ids_property_combines_xp_kind_only(
    buff_ids: tuple[str, ...],
) -> None:
    expected = 1.0
    for buff_id in buff_ids:
        buff = get_buff_definition(buff_id)
        if buff.kind == "xp":
            expected *= buff.multiplier

    result = xp_multiplier_for_buff_ids(buff_ids)

    assert isinstance(result, float)
    assert result == xp_multiplier_for_buff_ids(buff_ids)
    assert result == pytest.approx(expected)
    assert xp_multiplier_for_buff_ids(()) == 1.0


@settings(max_examples=75, deadline=None)
@given(
    remaining_quota_ratio=st.floats(
        min_value=0.0,
        max_value=1_000_000.0,
        allow_nan=False,
        allow_infinity=False,
        width=32,
    )
)
def test_routing_priority_quota_helper_property_matches_context_path(
    remaining_quota_ratio: float,
) -> None:
    expected = (
        CAP_WARNING_ROUTING_PRIORITY_MULTIPLIER
        if remaining_quota_ratio < CAP_WARNING_QUOTA_RATIO
        else 1.0
    )

    result = routing_priority_multiplier_for_quota_ratio(remaining_quota_ratio)

    assert isinstance(result, float)
    assert result == routing_priority_multiplier_for_quota_ratio(
        remaining_quota_ratio
    )
    assert result == pytest.approx(expected)
    assert result == pytest.approx(
        routing_priority_multiplier_for_context(
            BuffContext(
                now=datetime(1970, 1, 1, tzinfo=timezone.utc),
                remaining_quota_ratio=remaining_quota_ratio,
            )
        )
    )


def test_public_threshold_edges_remain_pinned() -> None:
    assert active_buff_ids_for_context(
        BuffContext(
            now=_NOW,
            last_rolling_5h_reset_at=_NOW
            - timedelta(seconds=FRESH_TOKENS_DURATION_SECONDS),
            last_task_completed_at=_NOW - timedelta(seconds=WELL_RESTED_IDLE_SECONDS),
            consecutive_successes=STREAK_SUCCESS_COUNT,
            remaining_quota_ratio=CAP_WARNING_QUOTA_RATIO,
        )
    ) == (FRESH_TOKENS_BUFF_ID, WELL_RESTED_BUFF_ID, STREAK_BUFF_ID)
    assert xp_multiplier_for_buff_ids(
        (FRESH_TOKENS_BUFF_ID, WELL_RESTED_BUFF_ID, STREAK_BUFF_ID)
    ) == pytest.approx(
        FRESH_TOKENS_XP_MULTIPLIER
        * WELL_RESTED_XP_MULTIPLIER
        * STREAK_XP_MULTIPLIER
    )
    assert routing_priority_multiplier_for_quota_ratio(
        CAP_WARNING_QUOTA_RATIO
    ) == 1.0

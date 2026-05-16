"""RPG.W3.1 -- contract tests for ``backend/agents/instance_suffix.py``.

Covers the W3 instance suffix allocator contract from ADR-0008:

1. First duplicate instance receives ``alpha``.
2. Existing reservations advance allocation to ``beta`` / ``gamma``.
3. Parallel spawn batches return unique suffixes in canonical order.
4. Exhausted canonical suffixes continue with deterministic numbered cycles.
5. Invalid allocation inputs are rejected before callers create card rows.
"""

from __future__ import annotations

import pytest

from backend.agents.instance_suffix import (
    CANONICAL_INSTANCE_SUFFIXES,
    InstanceSuffixAllocationError,
    allocate_instance_suffix,
    allocate_instance_suffixes,
)


def test_first_instance_suffix_is_alpha() -> None:
    assert allocate_instance_suffix() == "alpha"


def test_existing_reservations_advance_to_beta_and_gamma() -> None:
    assert allocate_instance_suffix(["alpha"]) == "beta"
    assert allocate_instance_suffix(["alpha", "beta"]) == "gamma"


def test_parallel_spawn_allocates_alpha_beta_gamma_without_duplicates() -> None:
    suffixes = allocate_instance_suffixes(spawn_count=3)
    assert suffixes == ("alpha", "beta", "gamma")
    assert len(set(suffixes)) == 3


def test_parallel_spawn_skips_reserved_suffixes_but_preserves_order() -> None:
    assert allocate_instance_suffixes(["alpha", "gamma"], spawn_count=3) == (
        "beta",
        "delta",
        "epsilon",
    )


def test_exhausted_canonical_suffixes_continue_deterministically() -> None:
    suffixes = allocate_instance_suffixes(CANONICAL_INSTANCE_SUFFIXES, spawn_count=3)
    assert suffixes == ("alpha-2", "beta-2", "gamma-2")


def test_reserved_suffixes_are_trimmed_and_deduped() -> None:
    assert allocate_instance_suffixes([" alpha ", "alpha", " beta "], 2) == (
        "gamma",
        "delta",
    )


@pytest.mark.parametrize("spawn_count", [0, -1])
def test_spawn_count_must_be_positive(spawn_count: int) -> None:
    with pytest.raises(InstanceSuffixAllocationError, match="spawn_count"):
        allocate_instance_suffixes(spawn_count=spawn_count)


@pytest.mark.parametrize("spawn_count", [True, 1.5, "3"])
def test_spawn_count_must_be_an_int(spawn_count: object) -> None:
    with pytest.raises(TypeError, match="spawn_count"):
        allocate_instance_suffixes(spawn_count=spawn_count)  # type: ignore[arg-type]


def test_reserved_suffixes_must_be_an_iterable_not_a_string() -> None:
    with pytest.raises(TypeError, match="reserved_suffixes"):
        allocate_instance_suffixes("alpha")  # type: ignore[arg-type]


def test_reserved_suffixes_must_be_non_empty_strings() -> None:
    with pytest.raises(InstanceSuffixAllocationError, match="reserved suffixes"):
        allocate_instance_suffixes(["alpha", " "])

    with pytest.raises(TypeError, match="reserved_suffixes"):
        allocate_instance_suffixes(["alpha", 42])  # type: ignore[list-item]

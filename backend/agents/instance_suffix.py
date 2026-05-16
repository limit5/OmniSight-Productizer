"""RPG.W3.1 -- deterministic agent instance suffix allocation.

ADR-0008 defines an agent identity as ``class x instance_suffix x
character_card`` and reserves ``alpha | beta | gamma | ...`` for parallel
spawn differentiation. This module keeps the allocation rule pure so callers
can reserve suffixes before creating durable character-card rows.

Module-global state audit (per project SOP)
-------------------------------------------
This module defines immutable constants only. It performs no database access,
no clock reads, and no runtime mutation; callers pass the already-reserved
suffixes explicitly.
"""

from __future__ import annotations

from collections.abc import Iterable

CANONICAL_INSTANCE_SUFFIXES: tuple[str, ...] = (
    "alpha",
    "beta",
    "gamma",
    "delta",
    "epsilon",
    "zeta",
    "eta",
    "theta",
    "iota",
    "kappa",
    "lambda",
    "mu",
    "nu",
    "xi",
    "omicron",
    "pi",
    "rho",
    "sigma",
    "tau",
    "upsilon",
    "phi",
    "chi",
    "psi",
    "omega",
)


class InstanceSuffixAllocationError(ValueError):
    """Raised when instance suffix allocation input is invalid."""


def allocate_instance_suffix(reserved_suffixes: Iterable[str] = ()) -> str:
    """Return the first suffix not present in ``reserved_suffixes``."""

    return allocate_instance_suffixes(reserved_suffixes, 1)[0]


def allocate_instance_suffixes(
    reserved_suffixes: Iterable[str] = (),
    spawn_count: int = 1,
) -> tuple[str, ...]:
    """Return ``spawn_count`` unique suffixes for a parallel spawn batch.

    The allocator always fills the canonical ADR-0008 sequence first
    (``alpha``, ``beta``, ``gamma``, ...). Once that sequence is exhausted it
    continues deterministically with numbered cycles such as ``alpha-2``.
    """

    count = _clean_spawn_count(spawn_count)
    reserved = _clean_reserved_suffixes(reserved_suffixes)
    allocated: list[str] = []
    candidate_index = 0

    while len(allocated) < count:
        candidate = _suffix_for_index(candidate_index)
        candidate_index += 1
        if candidate in reserved:
            continue
        reserved.add(candidate)
        allocated.append(candidate)

    return tuple(allocated)


def _clean_spawn_count(spawn_count: int) -> int:
    if isinstance(spawn_count, bool) or not isinstance(spawn_count, int):
        raise TypeError("spawn_count must be an int")
    if spawn_count < 1:
        raise InstanceSuffixAllocationError("spawn_count must be >= 1")
    return spawn_count


def _clean_reserved_suffixes(reserved_suffixes: Iterable[str]) -> set[str]:
    if isinstance(reserved_suffixes, str):
        raise TypeError("reserved_suffixes must be an iterable of strings")

    clean: set[str] = set()
    for suffix in reserved_suffixes:
        if not isinstance(suffix, str):
            raise TypeError("reserved_suffixes must contain only strings")
        value = suffix.strip()
        if not value:
            raise InstanceSuffixAllocationError("reserved suffixes must be non-empty")
        clean.add(value)
    return clean


def _suffix_for_index(index: int) -> str:
    cycle, offset = divmod(index, len(CANONICAL_INSTANCE_SUFFIXES))
    suffix = CANONICAL_INSTANCE_SUFFIXES[offset]
    if cycle == 0:
        return suffix
    return f"{suffix}-{cycle + 1}"


__all__ = [
    "CANONICAL_INSTANCE_SUFFIXES",
    "InstanceSuffixAllocationError",
    "allocate_instance_suffix",
    "allocate_instance_suffixes",
]

"""U6-0b — per-user memory scope + erasure state machine (DORMANT).

The scope + erasure CONTRACT for the U6 memory legs (frozen design §2.D + §11
RB2). NOTHING stores memory yet — U6-4 (L3 store) + U6-6 (erase) build on this.
Two frozen pieces:

  * ``MemoryScope`` = the composite ``(tenant_id, user_id)`` identity, with a
    COLLISION-FREE ``scope_key`` (length-framed, so ``("a:b","c")`` ≠
    ``("a","b:c")``) and a user-inclusive ``cache_key`` (cross-user cache
    confusion is structurally impossible). Isolation is enforced by these keys +
    per-row scope columns + RLS at the store layer (U6-4) — NOT by an opaque
    concatenated string (the v1 hole the audit rejected).
  * ``ErasureState`` = the ``ACTIVE → ERASING → ERASED`` state machine for
    crypto-shred (the actual key-destruction / ciphertext lifecycle lives in
    ``u6_memory_crypto`` + the U6-4 store; this pins the legal transitions).

Pure + offline; no DB, no crypto here (crypto is ``u6_memory_crypto``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """A per-user memory audience: ``(tenant_id, user_id)``. Both non-empty."""

    tenant_id: str
    user_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, str) or not self.tenant_id:
            raise ValueError("tenant_id must be a non-empty str")
        if not isinstance(self.user_id, str) or not self.user_id:
            raise ValueError("user_id must be a non-empty str")


def _frame(part: str) -> str:
    """Length-framed component so no delimiter/ambiguity collision is possible:
    ``("a:b","c")`` and ``("a","b:c")`` yield DIFFERENT frames."""
    return f"{len(part)}:{part}"


def scope_key(scope: MemoryScope) -> str:
    """The canonical, collision-free key for a VALID ``MemoryScope`` audience.
    Used as the partition/version/snapshot key of the U6 per-user stores.

    Rejects any non-``MemoryScope`` (a duck-typed row/ORM proxy would bypass the
    ``__post_init__`` non-empty check and yield a degenerate ``u6mem:0::0:`` key —
    the isolation boundary must self-defend, matching ``seal``/``envelope.decrypt``).
    A caller that deliberately forges an instance via ``object.__new__`` /
    subclassing to smuggle empty fields is out of threat model — it has already
    abandoned the constructor that is the sole validation seam.
    """
    if not isinstance(scope, MemoryScope):
        raise TypeError(f"scope must be a MemoryScope, got {type(scope).__name__}")
    return "u6mem:" + _frame(scope.tenant_id) + ":" + _frame(scope.user_id)


def cache_key(scope: MemoryScope, namespace: str) -> str:
    """A cache key that INCLUDES the user — a cross-user cache hit is
    structurally impossible for any valid ``MemoryScope`` (closes the audit's
    cache-confusion negative). ``scope_key`` enforces the type guard."""
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("namespace must be a non-empty str")
    return scope_key(scope) + ":" + _frame(namespace)


class ErasureState(Enum):
    """Crypto-shred lifecycle. ACTIVE→ERASING (shred requested; key destruction +
    row deletion in progress)→ERASED (per-user key destroyed; ciphertext
    unrecoverable; only content-free metadata survives). Terminal at ERASED —
    a re-created fact for the same user starts a NEW ACTIVE lifecycle, never
    resurrects an ERASED one."""

    ACTIVE = "active"
    ERASING = "erasing"
    ERASED = "erased"


# The ONLY legal transitions. ERASED is terminal; ERASING may retry itself
# (idempotent shred). No edge ever returns to ACTIVE (no un-erase).
# NOTE(U6-4): ERASING->ERASING is legal but ACTIVE->ACTIVE is NOT — a store that
# blindly re-asserts the current state on every heartbeat will raise on an
# already-ACTIVE row. The store must treat "already in target state" as a no-op
# for non-erasing states rather than calling assert_transition unconditionally.
_ALLOWED_TRANSITIONS: frozenset[tuple[ErasureState, ErasureState]] = frozenset(
    {
        (ErasureState.ACTIVE, ErasureState.ERASING),
        (ErasureState.ERASING, ErasureState.ERASING),
        (ErasureState.ERASING, ErasureState.ERASED),
    }
)


def can_transition(current: ErasureState, target: ErasureState) -> bool:
    """True iff ``current → target`` is a legal erasure transition."""
    return (current, target) in _ALLOWED_TRANSITIONS


def assert_transition(current: ErasureState, target: ErasureState) -> ErasureState:
    """Return ``target`` iff the transition is legal, else raise — fail-closed so
    a store can never illegally un-erase or skip ERASING."""
    if not can_transition(current, target):
        raise ValueError(f"illegal erasure transition: {current.value} -> {target.value}")
    return target

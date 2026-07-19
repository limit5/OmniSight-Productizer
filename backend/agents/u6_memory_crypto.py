"""U6-0b — per-user memory crypto-shred primitive (DORMANT).

Composes the ALREADY-AUDITED envelope encryption (``backend/security/envelope``)
for the U6 per-user memory legs (frozen design §2.D + §11 RB2). Every L3 payload
is stored as **ciphertext + a DEK ref**; the DEK ref (the wrapped data-key) is the
ONLY key to the ciphertext, so "delete my memory" = DELETE the DEK ref (+ the
rows) ⇒ the ciphertext is cryptographically **unrecoverable** (crypto-shred).
Only content-free metadata (counts, timestamps) survives.

This writes NO new crypto — it composes ``envelope.encrypt``/``decrypt`` (AES-GCM
DEK wrapped by the KMS KEK). The shred itself is a DATA-LIFECYCLE operation the
STORE performs (delete the dek_ref row); unrecoverability comes from the
envelope's design — the ciphertext envelope contains NO key material, the DEK
lives ONLY in the separately-stored, separately-deletable dek_ref. A FRESH DEK is
minted per ``seal`` (never a shared per-user key), so deleting one user's dek_refs
shreds exactly that user's payloads — no over-deletion. DORMANT: nothing seals
memory yet (U6-4 wires it).

GRANULARITY BOUNDARY (audited; U6-4/U6-6 obligations, NOT this primitive's):
  * The composed envelope authenticates to **tenant** granularity (its AAD binds
    ``(tenant_id, dek_id)``). Per-**user** READ isolation is the U6-4 store's job
    (per-row scope columns + RLS keyed by ``u6_memory_scope``); its DoD MUST
    include a cross-user-read-denied test. The crypto gives erasure granularity
    (fresh DEK/seal), not per-user read authority.
  * "Unrecoverable" holds only once the dek_ref is gone from **every** copy — DB
    backups / PITR / WAL / read-replicas / caches included. U6-4/ops MUST define
    "erased" so it is claimed only after the ref is purged within the stated RPO
    (or the DEK rotated so old backups are useless). This module cannot enforce
    that; it only guarantees the surviving ciphertext carries no key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from backend.agents.u6_memory_scope import MemoryScope
from backend.security import envelope
from backend.security.envelope import TenantDEKRef

_PURPOSE = "u6_memory_l3"

# The COMPLETE set of keys the envelope JSON is allowed to carry (mirrors
# ``envelope.encrypt`` at backend/security/envelope.py:189-196). ``dek`` is the
# opaque dek_id LABEL (also AAD-authenticated), never key bytes. This is an
# ALLOWLIST on purpose: ``ciphertext_carries_no_key`` fails CLOSED on ANY key
# outside this set — a future envelope field (key material or not) forces a
# re-audit of the shred invariant instead of silently passing a denylist.
_ENVELOPE_PUBLIC_KEYS = frozenset(
    {"fmt", "alg", "dek", "tid", "nonce_b64", "ciphertext_b64"}
)


@dataclass(frozen=True, slots=True)
class SealedMemory:
    """A crypto-shreddable per-user payload: the ``ciphertext`` + the ``dek_ref``
    that is the ONLY key to it. The store persists BOTH; crypto-shred DELETES the
    ``dek_ref`` (rendering ``ciphertext`` permanently unrecoverable)."""

    ciphertext: str
    dek_ref: dict


def seal(scope: MemoryScope, plaintext: str) -> SealedMemory:
    """Encrypt a per-user memory payload. The returned ``dek_ref`` IS the shred
    key: keep it to open the payload, destroy it to crypto-shred it."""
    if not isinstance(scope, MemoryScope):
        raise ValueError("scope must be a MemoryScope")
    ciphertext, dek_ref = envelope.encrypt(plaintext, scope.tenant_id, purpose=_PURPOSE)
    return SealedMemory(ciphertext=ciphertext, dek_ref=dek_ref.to_dict())


def open_sealed(sealed: SealedMemory) -> str:
    """Decrypt a sealed payload using its DEK ref. If the ref was crypto-shredded
    (destroyed / never stored), the ciphertext alone can NEVER be decrypted."""
    ref = TenantDEKRef.from_dict(sealed.dek_ref)
    return envelope.decrypt(sealed.ciphertext, ref)


def ciphertext_carries_no_key(ciphertext: str) -> bool:
    """Unrecoverability invariant (crypto-shred's linchpin): the ciphertext
    envelope must carry NO key material — the DEK lives ONLY in the dek_ref.
    Were this false, deleting the dek_ref would NOT shred the payload.

    Enforced as an ALLOWLIST (fails closed on any unexpected key), so it cannot be
    fooled by key material hidden under a field name a denylist never anticipated.
    """
    try:
        env = json.loads(ciphertext)
    except (ValueError, TypeError):
        return False
    return isinstance(env, dict) and set(env.keys()) <= _ENVELOPE_PUBLIC_KEYS

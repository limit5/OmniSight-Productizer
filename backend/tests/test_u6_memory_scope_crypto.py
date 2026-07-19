"""U6-0b — per-user memory scope + crypto-shred tests (offline, dormant).

Pins the frozen §2.D scope + erasure contract: collision-free per-user keys
(cross-tenant same-user, cache confusion, sentinel leak → all impossible by
construction), the ACTIVE→ERASING→ERASED state machine, and the crypto-shred
unrecoverability invariant (the ciphertext carries no key; the dek_ref is the
only key, so deleting it is permanent).
"""
from __future__ import annotations

import pytest

from backend.agents.u6_memory_crypto import (
    SealedMemory,
    ciphertext_carries_no_key,
    open_sealed,
    seal,
)
from backend.agents.u6_memory_scope import (
    ErasureState,
    MemoryScope,
    assert_transition,
    cache_key,
    can_transition,
    scope_key,
)
from backend.security.envelope import EnvelopeEncryptionError


# ── scope identity + non-empty ───────────────────────────────────────────
@pytest.mark.parametrize("tenant,user", [("", "u"), ("t", ""), ("", ""), (None, "u")])
def test_memory_scope_rejects_empty_or_bad(tenant, user) -> None:
    with pytest.raises(ValueError):
        MemoryScope(tenant_id=tenant, user_id=user)


# ── scope_key is COLLISION-FREE (the audit's core scope negatives) ────────
def test_scope_key_no_delimiter_collision() -> None:
    # ("a:b","c") and ("a","b:c") must NOT collide (the opaque-concat hole v1 had).
    a = scope_key(MemoryScope("a:b", "c"))
    b = scope_key(MemoryScope("a", "b:c"))
    assert a != b


def test_scope_key_same_user_across_tenants_differs() -> None:
    a = scope_key(MemoryScope("tenant-1", "shared-user"))
    b = scope_key(MemoryScope("tenant-2", "shared-user"))
    assert a != b


def test_scope_key_same_tenant_different_users_differ() -> None:
    a = scope_key(MemoryScope("t", "user-a"))
    b = scope_key(MemoryScope("t", "user-b"))
    assert a != b


def test_scope_keys_are_globally_distinct_no_sentinel_leak() -> None:
    scopes = [
        MemoryScope("t1", "u1"), MemoryScope("t1", "u2"),
        MemoryScope("t2", "u1"), MemoryScope("t:1", "u:2"),
        MemoryScope("t", "1:u:2"), MemoryScope("t1", "u1:extra"),
        # the SHARP framing cases: a field that itself looks like "N:..." framing,
        # and a field that embeds the literal "u6mem:" prefix.
        MemoryScope("3:xxx", "u"), MemoryScope("3:x", "xx:u"),
        MemoryScope("u6mem:1:t", "1:u"), MemoryScope("u6mem:1", "t:1:u"),
    ]
    keys = [scope_key(s) for s in scopes]
    assert len(set(keys)) == len(keys)  # every scope maps to its OWN key


def test_scope_key_and_cache_key_reject_non_memoryscope() -> None:
    """The isolation boundary self-defends: a duck-typed object with the right
    attributes must NOT yield a (degenerate) key — only a real MemoryScope does."""
    class Duck:  # looks like a scope, isn't one
        tenant_id = ""
        user_id = ""

    with pytest.raises(TypeError):
        scope_key(Duck())
    with pytest.raises(TypeError):
        cache_key(Duck(), "l3_facts")


# ── cache_key includes the user (no cross-user cache confusion) ───────────
def test_cache_key_includes_user_no_cross_user_hit() -> None:
    a = cache_key(MemoryScope("t", "user-a"), "l3_facts")
    b = cache_key(MemoryScope("t", "user-b"), "l3_facts")
    assert a != b


def test_cache_key_namespaced() -> None:
    s = MemoryScope("t", "u")
    assert cache_key(s, "l3_facts") != cache_key(s, "l2_summaries")
    assert cache_key(s, "l3_facts").startswith(scope_key(s))


def test_cache_key_rejects_empty_namespace() -> None:
    with pytest.raises(ValueError):
        cache_key(MemoryScope("t", "u"), "")


# ── ErasureState machine (ACTIVE → ERASING → ERASED, no un-erase) ─────────
@pytest.mark.parametrize(
    "cur,tgt",
    [
        (ErasureState.ACTIVE, ErasureState.ERASING),
        (ErasureState.ERASING, ErasureState.ERASING),
        (ErasureState.ERASING, ErasureState.ERASED),
    ],
)
def test_legal_erasure_transitions(cur, tgt) -> None:
    assert can_transition(cur, tgt) is True
    assert assert_transition(cur, tgt) is tgt


@pytest.mark.parametrize(
    "cur,tgt",
    [
        (ErasureState.ACTIVE, ErasureState.ERASED),   # can't skip ERASING
        (ErasureState.ACTIVE, ErasureState.ACTIVE),   # no self-loop on ACTIVE
        (ErasureState.ERASING, ErasureState.ACTIVE),  # NO un-erase
        (ErasureState.ERASED, ErasureState.ACTIVE),   # ERASED is terminal
        (ErasureState.ERASED, ErasureState.ERASING),
        (ErasureState.ERASED, ErasureState.ERASED),
    ],
)
def test_illegal_erasure_transitions_fail_closed(cur, tgt) -> None:
    assert can_transition(cur, tgt) is False
    with pytest.raises(ValueError):
        assert_transition(cur, tgt)


# ── crypto-shred: round-trip + unrecoverability ──────────────────────────
def test_seal_open_round_trip() -> None:
    scope = MemoryScope("omnisight-self", "user-42")
    sealed = seal(scope, "the user prefers named pipes")
    assert open_sealed(sealed) == "the user prefers named pipes"


def test_ciphertext_carries_no_key_material() -> None:
    sealed = seal(MemoryScope("t", "u"), "secret payload")
    # crypto-shred's linchpin: the DEK is NOT in the ciphertext — only in the ref.
    assert ciphertext_carries_no_key(sealed.ciphertext) is True


def test_ciphertext_carries_no_key_is_an_allowlist_fails_closed() -> None:
    """The check must FAIL CLOSED on any key outside the envelope's known set —
    so key material smuggled under an unanticipated field name can never pass."""
    import json as _json

    assert ciphertext_carries_no_key("not json at all") is False
    assert ciphertext_carries_no_key(_json.dumps([1, 2, 3])) is False  # not a dict
    # a well-formed envelope with ONE extra field (here obvious key material, but
    # the point is ANY unexpected key) is rejected:
    tampered = _json.dumps(
        {"fmt": "1", "alg": "AESGCM", "dek": "dek_x", "tid": "t",
         "nonce_b64": "AAAA", "ciphertext_b64": "BBBB", "raw_key": "deadbeef"}
    )
    assert ciphertext_carries_no_key(tampered) is False


def test_wrapped_dek_bytes_absent_from_ciphertext() -> None:
    """The STRONGEST, format-independent shred proof: the exact wrapped-DEK
    material that crypto-shred DELETES (``dek_ref['wrapped_dek_b64']``) does not
    appear anywhere in the surviving ciphertext. So once the ref is gone, no copy
    of the (wrapped) key remains with the ciphertext — nothing to unwrap."""
    sealed = seal(MemoryScope("t", "u"), "secret payload")
    wrapped = sealed.dek_ref["wrapped_dek_b64"]
    assert wrapped  # the ref really does carry the wrapped DEK...
    assert wrapped not in sealed.ciphertext  # ...and the ciphertext does NOT.


def test_shred_deleting_dek_ref_makes_payload_unrecoverable() -> None:
    scope = MemoryScope("t", "u")
    sealed = seal(scope, "personal data to be erasable")
    # With the ref, it opens.
    assert open_sealed(sealed) == "personal data to be erasable"
    # Crypto-shred = the store DELETES the dek_ref. The surviving ciphertext then
    # carries NO key, and opening with a destroyed/empty ref is impossible.
    assert ciphertext_carries_no_key(sealed.ciphertext) is True
    shredded = SealedMemory(ciphertext=sealed.ciphertext, dek_ref={})
    with pytest.raises(EnvelopeEncryptionError):
        open_sealed(shredded)


def test_shred_survivor_ciphertext_plus_shared_kek_recovers_nothing() -> None:
    """The strong witness: model exactly what an attacker retains AFTER a shred —
    the surviving ciphertext AND the (shared, NEVER-shredded) process KEK — and
    show it recovers nothing. Unrecoverability rides on the wrapped DEK being
    single-copy in the deleted ref, NOT on destroying the shared KEK."""
    from backend.security import kms_adapters

    sealed = seal(MemoryScope("t", "u"), "personal data")
    surviving_ciphertext = sealed.ciphertext  # store keeps this row
    _shared_kek_still_alive = kms_adapters.LocalFernetKMSAdapter()  # KEK not shredded
    # The ONLY wrapped DEK lived in dek_ref, which the store deleted:
    assert ciphertext_carries_no_key(surviving_ciphertext) is True
    assert sealed.dek_ref["wrapped_dek_b64"] not in surviving_ciphertext
    # With no dek_ref there is no wrapped DEK to unwrap — the live KEK is useless.
    with pytest.raises(EnvelopeEncryptionError):
        open_sealed(SealedMemory(ciphertext=surviving_ciphertext, dek_ref={}))


def test_corrupted_dek_ref_cannot_decrypt() -> None:
    """A structurally-VALID ref whose wrapped-DEK bytes are destroyed fails at the
    CRYPTO layer (not schema) — the complement to the empty-ref case above."""
    sealed = seal(MemoryScope("t", "u"), "x")
    bad = dict(sealed.dek_ref)
    bad["wrapped_dek_b64"] = "AAAA"  # garbage wrapped DEK (valid b64, wrong bytes)
    with pytest.raises(EnvelopeEncryptionError):
        open_sealed(SealedMemory(ciphertext=sealed.ciphertext, dek_ref=bad))


def test_distinct_seals_have_distinct_ciphertext() -> None:
    scope = MemoryScope("t", "u")
    a = seal(scope, "same plaintext")
    b = seal(scope, "same plaintext")
    # Fresh DEK + nonce per seal ⇒ ciphertexts differ even for identical input.
    assert a.ciphertext != b.ciphertext

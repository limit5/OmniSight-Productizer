"""AS.6.2 — `backend.security.credential_vault` contract tests.

Validates the generalised credential-vault primitive that the future
migrate-phase row will dual-write through (Track C of AS.0.4 §2). Pinned
invariants — same shape as :mod:`backend.tests.test_token_vault` so a
contributor reading either gets the same mental model:

1. Round-trip — ``encrypt_*(rid, tid, plaintext)`` →
   ``decrypt_*(rid, tid, …)`` returns the exact plaintext.
2. Per-row salt — same plaintext encrypts to a different ciphertext on
   each call (binding-envelope salt + Fernet IV both contribute).
3. Record-type whitelist — only the four AS.6.2 record types are
   accepted, and the per-domain helpers refuse the wrong domain's
   record type even though both go through the same internal envelope.
4. Binding mismatch — decrypt with the wrong tenant_id /
   resource_id / record_type / domain raises
   :class:`BindingMismatchError`. Defends against DB-level row
   shuffles (cross-tenant, cross-account, cross-column).
5. Cross-vault isolation — a ``token_vault`` ciphertext does NOT
   decrypt through ``credential_vault`` and vice versa (each owns its
   own envelope shape).
6. Key version — :data:`KEY_VERSION_CURRENT` is the only accepted
   version this release; anything else raises
   :class:`UnknownKeyVersionError` (AS.0.4 §3.1 reservation).
7. Ciphertext corruption — Fernet auth failures + malformed inner
   envelopes are translated to :class:`CiphertextCorruptedError`.
8. Single-master-key invariant — module source MUST go through
   ``backend.secret_store`` and MUST NOT mention ``Fernet.generate_key``
   or any ``OMNISIGHT_*_SECRET_KEY`` env var (AS.0.4 §3.1 / §5.4 grep
   guard, generalised across record types).
9. Module-global state audit — per SOP §1: no module-level mutable
   state, constants stable across :func:`importlib.reload`, no IO at
   import time, ``import secrets`` provenance grep.
10. AS.0.8 single-knob — :func:`is_enabled` reads
    ``settings.as_enabled`` via getattr fallback (forward-promotion
    guard).
11. Empty / wrong-type inputs — vault refuses empty plaintext, missing
    tenant_id / resource_id, non-dataclass on decrypt.
12. Migrate-phase forward-reservation — the rewrap helper accepts a
    legacy ``secret_store.encrypt`` ciphertext and produces an
    :class:`EncryptedSecret` with the binding envelope.
13. Expand-phase contract — neither ``git_credentials`` nor
    ``llm_credentials`` import this module yet (the migrate-phase row
    will flip those imports on).
"""

from __future__ import annotations

import importlib
import inspect
import json
import pathlib

import pytest

from backend import secret_store
from backend.security import credential_vault as cv
from backend.security import token_vault as tv


# Constants used across families
TENANT_A = "t-acme"
TENANT_B = "t-beta"
ACCOUNT_A = "ga-aaaa"
ACCOUNT_B = "ga-bbbb"
LLM_A = "lc-1111"
LLM_B = "lc-2222"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — round-trip (git + llm)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize(
    "secret_kind",
    [
        cv.RECORD_GIT_TOKEN,
        cv.RECORD_GIT_SSH_KEY,
        cv.RECORD_GIT_WEBHOOK_SECRET,
    ],
)
def test_round_trip_each_git_secret_kind(secret_kind: str) -> None:
    plaintext = f"git-secret-for-{secret_kind}-" + ("x" * 30)
    enc = cv.encrypt_git_secret(
        account_id=ACCOUNT_A,
        tenant_id=TENANT_A,
        secret_kind=secret_kind,
        plaintext=plaintext,
    )
    assert isinstance(enc, cv.EncryptedSecret)
    assert enc.key_version == cv.KEY_VERSION_CURRENT
    assert cv.decrypt_git_secret(
        account_id=ACCOUNT_A,
        tenant_id=TENANT_A,
        secret_kind=secret_kind,
        secret=enc,
    ) == plaintext


def test_round_trip_llm_credential() -> None:
    plaintext = "sk-ant-api03-abcdef1234567890"
    enc = cv.encrypt_llm_credential(
        credential_id=LLM_A,
        tenant_id=TENANT_A,
        plaintext=plaintext,
    )
    assert cv.decrypt_llm_credential(
        credential_id=LLM_A,
        tenant_id=TENANT_A,
        secret=enc,
    ) == plaintext


def test_round_trip_long_unicode_plaintext() -> None:
    plaintext = "ssh-ed25519 AAAA " + "𓀀𓁀𓂀𓃀" * 50 + "—comment"
    enc = cv.encrypt_git_secret(
        account_id=ACCOUNT_A,
        tenant_id=TENANT_A,
        secret_kind=cv.RECORD_GIT_SSH_KEY,
        plaintext=plaintext,
    )
    assert cv.decrypt_git_secret(
        account_id=ACCOUNT_A,
        tenant_id=TENANT_A,
        secret_kind=cv.RECORD_GIT_SSH_KEY,
        secret=enc,
    ) == plaintext


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — per-row salt
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_per_row_salt_uniqueness_git() -> None:
    """Same (account_id, tenant_id, secret_kind, plaintext) across N
    encrypts → N distinct ciphertexts. Defence-in-depth on top of
    Fernet's random IV."""
    seen: set[str] = set()
    for _ in range(20):
        enc = cv.encrypt_git_secret(
            account_id=ACCOUNT_A,
            tenant_id=TENANT_A,
            secret_kind=cv.RECORD_GIT_TOKEN,
            plaintext="same-plaintext",
        )
        assert enc.ciphertext not in seen
        seen.add(enc.ciphertext)


def test_per_row_salt_uniqueness_llm() -> None:
    seen: set[str] = set()
    for _ in range(20):
        enc = cv.encrypt_llm_credential(
            credential_id=LLM_A,
            tenant_id=TENANT_A,
            plaintext="same-plaintext",
        )
        assert enc.ciphertext not in seen
        seen.add(enc.ciphertext)


def test_salt_lives_inside_envelope_not_in_dataclass() -> None:
    """Per AS.6.2 module docstring + AS.2.1 contract: the per-row salt
    MUST live inside the Fernet-authenticated envelope, NOT as a
    public attribute."""
    enc = cv.encrypt_git_secret(
        account_id=ACCOUNT_A,
        tenant_id=TENANT_A,
        secret_kind=cv.RECORD_GIT_TOKEN,
        plaintext="tok",
    )
    assert set(vars(enc).keys()) == {"ciphertext", "key_version"}


def test_envelope_shape_pin_git() -> None:
    """Pin the envelope shape for the git domain so future contributors
    cannot silently add / drop fields. The migrate-phase row will read
    these field names verbatim."""
    enc = cv.encrypt_git_secret(
        account_id=ACCOUNT_A,
        tenant_id=TENANT_A,
        secret_kind=cv.RECORD_GIT_TOKEN,
        plaintext="tok-peek",
    )
    raw = secret_store.decrypt(enc.ciphertext)
    envelope = json.loads(raw)
    assert envelope["fmt"] == cv.BINDING_FORMAT_VERSION
    assert envelope["rec"] == cv.RECORD_GIT_TOKEN
    assert envelope["tid"] == TENANT_A
    assert envelope["rid"] == ACCOUNT_A
    assert envelope["tok"] == "tok-peek"
    assert isinstance(envelope["salt"], str) and len(envelope["salt"]) >= 16


def test_envelope_shape_pin_llm() -> None:
    enc = cv.encrypt_llm_credential(
        credential_id=LLM_A,
        tenant_id=TENANT_A,
        plaintext="api-key",
    )
    envelope = json.loads(secret_store.decrypt(enc.ciphertext))
    assert envelope["rec"] == cv.RECORD_LLM_VALUE
    assert envelope["rid"] == LLM_A
    assert envelope["tid"] == TENANT_A


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — record-type whitelist + cross-domain isolation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_supported_record_types_pinned() -> None:
    assert cv.SUPPORTED_RECORD_TYPES == frozenset(
        {
            "git_token",
            "git_ssh_key",
            "git_webhook_secret",
            "llm_value",
        }
    )


def test_supported_record_types_partition_into_domains() -> None:
    """Git and llm record-type sets MUST partition the full whitelist
    with no overlap — a single record type cannot belong to both
    domains, otherwise the per-domain helpers would silently accept
    each other's tokens."""
    assert cv.SUPPORTED_GIT_RECORD_TYPES.isdisjoint(
        cv.SUPPORTED_LLM_RECORD_TYPES
    )
    union = cv.SUPPORTED_GIT_RECORD_TYPES | cv.SUPPORTED_LLM_RECORD_TYPES
    assert union == cv.SUPPORTED_RECORD_TYPES


def test_git_helper_rejects_llm_record_type() -> None:
    """``encrypt_git_secret`` refuses ``RECORD_LLM_VALUE`` even though
    the underlying envelope could carry it — domain isolation is
    enforced at the public seam, not deep inside."""
    with pytest.raises(cv.UnsupportedRecordTypeError):
        cv.encrypt_git_secret(
            account_id=ACCOUNT_A,
            tenant_id=TENANT_A,
            secret_kind=cv.RECORD_LLM_VALUE,
            plaintext="x",
        )


def test_unknown_record_type_rejected() -> None:
    with pytest.raises(cv.UnsupportedRecordTypeError):
        cv.encrypt_git_secret(
            account_id=ACCOUNT_A,
            tenant_id=TENANT_A,
            secret_kind="codesign_record",  # not yet whitelisted
            plaintext="x",
        )


def test_unsupported_record_type_error_subclasses_value_error() -> None:
    """Existing call sites that catch ``ValueError`` for input
    validation must keep working."""
    assert issubclass(cv.UnsupportedRecordTypeError, ValueError)


def test_empty_record_type_rejected() -> None:
    with pytest.raises(cv.UnsupportedRecordTypeError):
        cv.encrypt_git_secret(
            account_id=ACCOUNT_A,
            tenant_id=TENANT_A,
            secret_kind="",
            plaintext="x",
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 4 — binding mismatch (anti-shuffle)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_binding_mismatch_on_tenant_swap_git() -> None:
    enc = cv.encrypt_git_secret(
        account_id=ACCOUNT_A,
        tenant_id=TENANT_A,
        secret_kind=cv.RECORD_GIT_TOKEN,
        plaintext="tok",
    )
    with pytest.raises(cv.BindingMismatchError):
        cv.decrypt_git_secret(
            account_id=ACCOUNT_A,
            tenant_id=TENANT_B,
            secret_kind=cv.RECORD_GIT_TOKEN,
            secret=enc,
        )


def test_binding_mismatch_on_account_swap_git() -> None:
    enc = cv.encrypt_git_secret(
        account_id=ACCOUNT_A,
        tenant_id=TENANT_A,
        secret_kind=cv.RECORD_GIT_TOKEN,
        plaintext="tok",
    )
    with pytest.raises(cv.BindingMismatchError):
        cv.decrypt_git_secret(
            account_id=ACCOUNT_B,
            tenant_id=TENANT_A,
            secret_kind=cv.RECORD_GIT_TOKEN,
            secret=enc,
        )


def test_binding_mismatch_on_secret_kind_swap_git() -> None:
    """Concrete attack: attacker swaps account-A's encrypted_token
    column value into the encrypted_ssh_key column. Decrypt of the
    ssh_key column must fail even though the ciphertext is valid
    Fernet."""
    enc = cv.encrypt_git_secret(
        account_id=ACCOUNT_A,
        tenant_id=TENANT_A,
        secret_kind=cv.RECORD_GIT_TOKEN,
        plaintext="tok",
    )
    with pytest.raises(cv.BindingMismatchError):
        cv.decrypt_git_secret(
            account_id=ACCOUNT_A,
            tenant_id=TENANT_A,
            secret_kind=cv.RECORD_GIT_SSH_KEY,
            secret=enc,
        )


def test_binding_mismatch_on_tenant_swap_llm() -> None:
    enc = cv.encrypt_llm_credential(
        credential_id=LLM_A,
        tenant_id=TENANT_A,
        plaintext="key",
    )
    with pytest.raises(cv.BindingMismatchError):
        cv.decrypt_llm_credential(
            credential_id=LLM_A,
            tenant_id=TENANT_B,
            secret=enc,
        )


def test_binding_mismatch_on_credential_swap_llm() -> None:
    enc = cv.encrypt_llm_credential(
        credential_id=LLM_A,
        tenant_id=TENANT_A,
        plaintext="key",
    )
    with pytest.raises(cv.BindingMismatchError):
        cv.decrypt_llm_credential(
            credential_id=LLM_B,
            tenant_id=TENANT_A,
            secret=enc,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 5 — cross-vault isolation (token_vault ⇄ credential_vault)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_token_vault_ciphertext_does_not_decrypt_through_credential_vault() -> None:
    """An OAuth ``EncryptedToken`` produced by token_vault must NOT
    decrypt through credential_vault — the envelope shape is different
    and the binding fields ``uid`` / ``prv`` will be missing the
    credential_vault-required ``rec`` / ``tid`` / ``rid`` fields."""
    oauth = tv.encrypt_for_user("user-42", "google", "ya29.tok")
    fake = cv.EncryptedSecret(
        ciphertext=oauth.ciphertext,
        key_version=oauth.key_version,
    )
    with pytest.raises(
        (cv.BindingMismatchError, cv.CiphertextCorruptedError)
    ):
        cv.decrypt_git_secret(
            account_id=ACCOUNT_A,
            tenant_id=TENANT_A,
            secret_kind=cv.RECORD_GIT_TOKEN,
            secret=fake,
        )


def test_credential_vault_ciphertext_does_not_decrypt_through_token_vault() -> None:
    """The reverse — a credential_vault ciphertext fed into
    token_vault must fail at envelope-shape validation (missing
    ``uid`` / ``prv`` fields)."""
    enc = cv.encrypt_git_secret(
        account_id=ACCOUNT_A,
        tenant_id=TENANT_A,
        secret_kind=cv.RECORD_GIT_TOKEN,
        plaintext="tok",
    )
    fake = tv.EncryptedToken(
        ciphertext=enc.ciphertext,
        key_version=enc.key_version,
    )
    with pytest.raises(
        (tv.BindingMismatchError, tv.CiphertextCorruptedError)
    ):
        tv.decrypt_for_user("user-42", "google", fake)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 6 — key_version reservation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_key_version_current_is_one() -> None:
    """AS.0.4 §3.1: this release ships with key_version=1."""
    assert cv.KEY_VERSION_CURRENT == 1


def test_key_version_matches_token_vault() -> None:
    """Both vaults bump key_version together when the future KMS
    rotation lands. Pinning equality here forces a coordinated update
    across both modules in the same PR."""
    assert cv.KEY_VERSION_CURRENT == tv.KEY_VERSION_CURRENT


def test_unknown_key_version_rejected() -> None:
    enc = cv.encrypt_llm_credential(
        credential_id=LLM_A,
        tenant_id=TENANT_A,
        plaintext="x",
    )
    fake = cv.EncryptedSecret(ciphertext=enc.ciphertext, key_version=2)
    with pytest.raises(cv.UnknownKeyVersionError):
        cv.decrypt_llm_credential(
            credential_id=LLM_A,
            tenant_id=TENANT_A,
            secret=fake,
        )


def test_zero_key_version_rejected() -> None:
    enc = cv.encrypt_llm_credential(
        credential_id=LLM_A,
        tenant_id=TENANT_A,
        plaintext="x",
    )
    fake = cv.EncryptedSecret(ciphertext=enc.ciphertext, key_version=0)
    with pytest.raises(cv.UnknownKeyVersionError):
        cv.decrypt_llm_credential(
            credential_id=LLM_A,
            tenant_id=TENANT_A,
            secret=fake,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 7 — ciphertext corruption
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_garbage_ciphertext_raises_corrupted() -> None:
    fake = cv.EncryptedSecret(ciphertext="not-a-real-fernet-token", key_version=1)
    with pytest.raises(cv.CiphertextCorruptedError):
        cv.decrypt_llm_credential(
            credential_id=LLM_A,
            tenant_id=TENANT_A,
            secret=fake,
        )


def test_tampered_ciphertext_raises_corrupted() -> None:
    enc = cv.encrypt_llm_credential(
        credential_id=LLM_A,
        tenant_id=TENANT_A,
        plaintext="x",
    )
    tampered = enc.ciphertext[:-2] + (
        "AA" if enc.ciphertext[-2:] != "AA" else "BB"
    )
    fake = cv.EncryptedSecret(ciphertext=tampered, key_version=1)
    with pytest.raises(cv.CiphertextCorruptedError):
        cv.decrypt_llm_credential(
            credential_id=LLM_A,
            tenant_id=TENANT_A,
            secret=fake,
        )


def test_envelope_with_unknown_format_raises_binding_mismatch() -> None:
    bogus = json.dumps(
        {
            "fmt": 99,
            "rec": cv.RECORD_LLM_VALUE,
            "tid": TENANT_A,
            "rid": LLM_A,
            "tok": "x",
            "salt": "AA==",
        }
    )
    fake = cv.EncryptedSecret(
        ciphertext=secret_store.encrypt(bogus),
        key_version=1,
    )
    with pytest.raises(cv.BindingMismatchError):
        cv.decrypt_llm_credential(
            credential_id=LLM_A,
            tenant_id=TENANT_A,
            secret=fake,
        )


def test_envelope_missing_rec_raises_corrupted() -> None:
    bogus = json.dumps(
        {
            "fmt": 1,
            "tid": TENANT_A,
            "rid": LLM_A,
            "tok": "x",
            "salt": "AA==",
        }
    )
    fake = cv.EncryptedSecret(
        ciphertext=secret_store.encrypt(bogus),
        key_version=1,
    )
    with pytest.raises(cv.CiphertextCorruptedError):
        cv.decrypt_llm_credential(
            credential_id=LLM_A,
            tenant_id=TENANT_A,
            secret=fake,
        )


def test_non_dict_envelope_raises_corrupted() -> None:
    fake = cv.EncryptedSecret(
        ciphertext=secret_store.encrypt(json.dumps(["just", "a", "list"])),
        key_version=1,
    )
    with pytest.raises(cv.CiphertextCorruptedError):
        cv.decrypt_llm_credential(
            credential_id=LLM_A,
            tenant_id=TENANT_A,
            secret=fake,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 8 — input validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_empty_plaintext_rejected() -> None:
    with pytest.raises(cv.CredentialVaultError):
        cv.encrypt_git_secret(
            account_id=ACCOUNT_A,
            tenant_id=TENANT_A,
            secret_kind=cv.RECORD_GIT_TOKEN,
            plaintext="",
        )


def test_non_string_plaintext_rejected() -> None:
    with pytest.raises(cv.CredentialVaultError):
        cv.encrypt_llm_credential(
            credential_id=LLM_A,
            tenant_id=TENANT_A,
            plaintext=12345,  # type: ignore[arg-type]
        )


def test_empty_tenant_id_rejected() -> None:
    with pytest.raises(cv.CredentialVaultError):
        cv.encrypt_git_secret(
            account_id=ACCOUNT_A,
            tenant_id="",
            secret_kind=cv.RECORD_GIT_TOKEN,
            plaintext="tok",
        )


def test_empty_resource_id_rejected() -> None:
    with pytest.raises(cv.CredentialVaultError):
        cv.encrypt_git_secret(
            account_id="",
            tenant_id=TENANT_A,
            secret_kind=cv.RECORD_GIT_TOKEN,
            plaintext="tok",
        )


def test_decrypt_rejects_non_dataclass_input() -> None:
    with pytest.raises(cv.CredentialVaultError):
        cv.decrypt_git_secret(
            account_id=ACCOUNT_A,
            tenant_id=TENANT_A,
            secret_kind=cv.RECORD_GIT_TOKEN,
            secret="raw-string-not-a-dataclass",  # type: ignore[arg-type]
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 9 — fingerprint re-export
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_fingerprint_matches_secret_store() -> None:
    samples = ["", "abc", "ghp_abc123def456ghi789jkl"]
    for s in samples:
        assert cv.fingerprint(s) == secret_store.fingerprint(s or "")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 10 — single-master-key invariant (AS.0.4 §3.1 / §5.4)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_credential_vault_uses_secret_store_fernet() -> None:
    """AS.0.4 §3 hard invariant: vault MUST go through
    ``backend.secret_store`` (single master Fernet key) and MUST NOT
    introduce a second master key — generalised across record types
    so the future codesign-track expansion can't sneak in a second
    key either."""
    src = inspect.getsource(cv)
    assert (
        "from backend import secret_store" in src
        or "from backend.secret_store" in src
    ), "vault must reuse backend.secret_store master key"
    assert "Fernet.generate_key" not in src, (
        "vault must not introduce a second master key — see AS.0.4 §3"
    )
    for forbidden_env in (
        "OMNISIGHT_OAUTH_SECRET_KEY",
        "OMNISIGHT_GIT_SECRET_KEY",
        "OMNISIGHT_LLM_SECRET_KEY",
        "OMNISIGHT_VAULT_SECRET_KEY",
    ):
        assert forbidden_env not in src, (
            f"vault must not introduce {forbidden_env} — see AS.0.4 §3"
        )
    for forbidden_kdf in ("HKDF", "PBKDF2", "Scrypt", "derive_key"):
        assert forbidden_kdf not in src, (
            f"vault must not derive sub-keys ({forbidden_kdf!r}) — "
            "see AS.0.4 §3.1"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 11 — module-global state audit (SOP §1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_no_module_level_mutable_state() -> None:
    forbidden = (list, dict, set)
    for name, value in vars(cv).items():
        if name.startswith("_"):
            continue
        if callable(value):
            continue
        if isinstance(value, type):
            continue
        assert not isinstance(value, forbidden), (
            f"module-level {name} is mutable {type(value).__name__} — "
            "SOP §1 forbids cross-worker drift surfaces"
        )


def test_constants_stable_across_reload() -> None:
    before = (
        cv.KEY_VERSION_CURRENT,
        cv.BINDING_FORMAT_VERSION,
        frozenset(cv.SUPPORTED_RECORD_TYPES),
        frozenset(cv.SUPPORTED_GIT_RECORD_TYPES),
        frozenset(cv.SUPPORTED_LLM_RECORD_TYPES),
    )
    reloaded = importlib.reload(cv)
    after = (
        reloaded.KEY_VERSION_CURRENT,
        reloaded.BINDING_FORMAT_VERSION,
        frozenset(reloaded.SUPPORTED_RECORD_TYPES),
        frozenset(reloaded.SUPPORTED_GIT_RECORD_TYPES),
        frozenset(reloaded.SUPPORTED_LLM_RECORD_TYPES),
    )
    assert before == after


def test_import_secrets_provenance() -> None:
    src = inspect.getsource(cv)
    assert "import secrets" in src, (
        "vault must use secrets module for salts"
    )
    assert "import random" not in src, (
        "vault must not import random — use secrets for cryptographic randomness"
    )


def test_public_surface_matches_all() -> None:
    expected = {
        "BINDING_FORMAT_VERSION",
        "BindingMismatchError",
        "CiphertextCorruptedError",
        "CredentialVaultError",
        "EncryptedSecret",
        "KEY_VERSION_CURRENT",
        "RECORD_GIT_SSH_KEY",
        "RECORD_GIT_TOKEN",
        "RECORD_GIT_WEBHOOK_SECRET",
        "RECORD_LLM_VALUE",
        "SUPPORTED_GIT_RECORD_TYPES",
        "SUPPORTED_LLM_RECORD_TYPES",
        "SUPPORTED_RECORD_TYPES",
        "UnknownKeyVersionError",
        "UnsupportedRecordTypeError",
        "decrypt_git_secret",
        "decrypt_llm_credential",
        "encrypt_git_secret",
        "encrypt_llm_credential",
        "fingerprint",
        "is_enabled",
        "migrate_legacy_secret_store_ciphertext",
    }
    assert set(cv.__all__) == expected
    for name in expected:
        assert hasattr(cv, name), f"{name} declared in __all__ but missing"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 12 — AS.0.8 single-knob hook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_is_enabled_returns_bool() -> None:
    assert isinstance(cv.is_enabled(), bool)


def test_is_enabled_default_true_when_setting_absent() -> None:
    try:
        from backend.config import settings
    except Exception:
        pytest.skip("config module unavailable")
    if hasattr(settings, "as_enabled"):
        assert cv.is_enabled() == bool(settings.as_enabled)
    else:
        assert cv.is_enabled() is True


def test_pure_helpers_callable_when_knob_off() -> None:
    """AS.0.4 §6.2 + module docstring: pure encrypt / decrypt helpers
    must keep working knob-off so backfill / DSAR / key-rotation
    scripts run regardless. Caller endpoints (future migrate-phase
    row) gate on :func:`is_enabled` before reaching the vault, not
    the vault itself."""
    enc = cv.encrypt_llm_credential(
        credential_id=LLM_A,
        tenant_id=TENANT_A,
        plaintext="key",
    )
    assert cv.decrypt_llm_credential(
        credential_id=LLM_A,
        tenant_id=TENANT_A,
        secret=enc,
    ) == "key"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 13 — migrate-phase forward-reservation helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_migrate_legacy_returns_none_for_empty_ciphertext() -> None:
    """Skip-row contract: empty / None legacy ciphertext returns None
    so the migrate scan can iterate over rows whose secret is unset."""
    assert cv.migrate_legacy_secret_store_ciphertext(
        record_type=cv.RECORD_GIT_TOKEN,
        tenant_id=TENANT_A,
        resource_id=ACCOUNT_A,
        legacy_ciphertext="",
    ) is None
    assert cv.migrate_legacy_secret_store_ciphertext(
        record_type=cv.RECORD_GIT_TOKEN,
        tenant_id=TENANT_A,
        resource_id=ACCOUNT_A,
        legacy_ciphertext=None,  # type: ignore[arg-type]
    ) is None


def test_migrate_legacy_round_trip_git() -> None:
    """The rewrap accepts a legacy plain ``secret_store.encrypt``
    ciphertext — the kind ``git_credentials.py`` writes today — and
    produces a binding-envelope ciphertext that decrypts cleanly
    through :func:`decrypt_git_secret`."""
    legacy_plain = "ghp_legacyToken1234567890abcdef"
    legacy_ct = secret_store.encrypt(legacy_plain)
    rewrapped = cv.migrate_legacy_secret_store_ciphertext(
        record_type=cv.RECORD_GIT_TOKEN,
        tenant_id=TENANT_A,
        resource_id=ACCOUNT_A,
        legacy_ciphertext=legacy_ct,
    )
    assert isinstance(rewrapped, cv.EncryptedSecret)
    assert cv.decrypt_git_secret(
        account_id=ACCOUNT_A,
        tenant_id=TENANT_A,
        secret_kind=cv.RECORD_GIT_TOKEN,
        secret=rewrapped,
    ) == legacy_plain


def test_migrate_legacy_round_trip_llm() -> None:
    legacy_plain = "sk-legacy-anthropic-key-1234"
    legacy_ct = secret_store.encrypt(legacy_plain)
    rewrapped = cv.migrate_legacy_secret_store_ciphertext(
        record_type=cv.RECORD_LLM_VALUE,
        tenant_id=TENANT_A,
        resource_id=LLM_A,
        legacy_ciphertext=legacy_ct,
    )
    assert rewrapped is not None
    assert cv.decrypt_llm_credential(
        credential_id=LLM_A,
        tenant_id=TENANT_A,
        secret=rewrapped,
    ) == legacy_plain


def test_migrate_legacy_rejects_garbage_ciphertext() -> None:
    with pytest.raises(cv.CiphertextCorruptedError):
        cv.migrate_legacy_secret_store_ciphertext(
            record_type=cv.RECORD_GIT_TOKEN,
            tenant_id=TENANT_A,
            resource_id=ACCOUNT_A,
            legacy_ciphertext="not-fernet",
        )


def test_migrate_legacy_rejects_unknown_record_type() -> None:
    legacy_ct = secret_store.encrypt("x")
    with pytest.raises(cv.UnsupportedRecordTypeError):
        cv.migrate_legacy_secret_store_ciphertext(
            record_type="codesign_record",
            tenant_id=TENANT_A,
            resource_id="any",
            legacy_ciphertext=legacy_ct,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 14 — expand-phase contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_git_credentials_does_not_import_credential_vault() -> None:
    """AS.0.4 §6.1 expand-phase acceptance: the new helper module is
    pure additive — no caller wired. ``git_credentials.py`` continues
    to call ``secret_store.encrypt`` directly. The migrate-phase row
    will flip this; this test fails red on that PR by design,
    advertising the cutover."""
    src = pathlib.Path(
        "backend/git_credentials.py"
    ).read_text(encoding="utf-8")
    assert "credential_vault" not in src, (
        "AS.6.2 expand-phase: git_credentials.py must NOT import "
        "credential_vault yet (the migrate-phase row flips this)"
    )


def test_llm_credentials_does_not_import_credential_vault() -> None:
    """Same expand-phase invariant for the LLM track."""
    src = pathlib.Path(
        "backend/llm_credentials.py"
    ).read_text(encoding="utf-8")
    assert "credential_vault" not in src, (
        "AS.6.2 expand-phase: llm_credentials.py must NOT import "
        "credential_vault yet (the migrate-phase row flips this)"
    )


def test_module_docstring_pins_migrate_phase_plan() -> None:
    """The expand-phase deliverable includes a docstring section
    documenting how the migrate-phase row is expected to dual-write /
    dual-read. Future contributors land on the file and immediately
    see the multi-row plan."""
    doc = cv.__doc__ or ""
    assert "Migrate-phase plan" in doc
    assert "migrate_legacy_secret_store_ciphertext" in doc
    assert "AS.0.4" in doc
    assert "Track C" in doc


def test_credential_vault_listed_in_security_namespace() -> None:
    """``backend.security.__all__`` exports the submodule so callers
    can ``from backend import security; security.credential_vault…``
    without a deeper import path."""
    from backend import security
    assert "credential_vault" in security.__all__
    assert security.credential_vault is cv

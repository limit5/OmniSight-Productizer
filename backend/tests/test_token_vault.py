"""AS.2.1 — `backend.security.token_vault` contract tests.

Validates the OAuth token-vault primitive that AS.2.2 (alembic 0057
``oauth_tokens``) and AS.6.x (OAuth router endpoints) will round-trip
through. Pinned invariants:

1. Round-trip — encrypt(uid, prv, tok) → decrypt(uid, prv, …) returns
   the exact plaintext.
2. Per-row salt — same plaintext encrypts to a different ciphertext on
   each call (binding-envelope salt + Fernet IV both contribute).
3. Provider whitelist — only the four AS.1 providers are accepted, and
   the whitelist byte-equals ``account_linking._AS1_OAUTH_PROVIDERS``
   (AS.0.4 §5.2 cross-module drift guard).
4. Binding mismatch — decrypt with the wrong user_id / wrong provider
   raises :class:`BindingMismatchError`. Defends against DB-level row
   shuffles.
5. Key version — :data:`KEY_VERSION_CURRENT` is the only accepted
   version this release; anything else raises
   :class:`UnknownKeyVersionError` (AS.0.4 §3.1 reservation).
6. Ciphertext corruption — Fernet auth failures + malformed inner
   envelopes are translated to :class:`CiphertextCorruptedError`.
7. Single-master-key invariant — module source MUST go through
   ``backend.secret_store`` and MUST NOT mention ``Fernet.generate_key``
   or ``OMNISIGHT_OAUTH_SECRET_KEY`` (AS.0.4 §5.4 grep guard).
8. Module-global state audit — per SOP §1: no module-level mutable
   state, constants stable across :func:`importlib.reload`, no IO at
   import time, ``import secrets`` provenance grep (mirrors AS.1.1).
9. AS.0.8 single-knob — :func:`is_enabled` reads ``settings.as_enabled``
   via getattr fallback (forward-promotion guard, mirrors AS.0.9 §7.2.6).
10. Empty / wrong-type inputs — vault refuses empty plaintext, missing
    user_id, non-string token wrapper.
"""

from __future__ import annotations

import importlib
import inspect
import json
import pathlib

import pytest

from backend import account_linking, secret_store
from backend.security import token_vault as tv


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — round-trip
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("provider", ["google", "github", "apple", "microsoft"])
def test_round_trip_each_provider(provider: str) -> None:
    plaintext = f"tok-for-{provider}-{'x' * 40}"
    encrypted = tv.encrypt_for_user("user-42", provider, plaintext)
    assert isinstance(encrypted, tv.EncryptedToken)
    assert encrypted.key_version == tv.KEY_VERSION_CURRENT
    decrypted = tv.decrypt_for_user("user-42", provider, encrypted)
    assert decrypted == plaintext


def test_round_trip_long_unicode_plaintext() -> None:
    plaintext = "ya29.a0Af " + "𝓞𝓶𝓷𝓲𝓢𝓲𝓰𝓱𝓽" * 20 + "—嘿嘿"
    encrypted = tv.encrypt_for_user("u-utf8", "google", plaintext)
    assert tv.decrypt_for_user("u-utf8", "google", encrypted) == plaintext


def test_provider_case_normalised_on_input() -> None:
    """Caller may pass mixed-case / whitespace; vault normalises to the
    canonical lowercase slug for binding."""
    encrypted = tv.encrypt_for_user("u1", " GitHub ", "ghp_abc123def456")
    # Decrypt must use the canonical slug (caller MUST NOT rely on the
    # whitespace input round-tripping); test the canonical form.
    assert tv.decrypt_for_user("u1", "github", encrypted) == "ghp_abc123def456"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — per-row salt
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_per_row_salt_uniqueness() -> None:
    """Same (user_id, provider, plaintext) across N encrypts → N
    distinct ciphertexts. Defence-in-depth on top of Fernet's random IV.
    """
    seen: set[str] = set()
    for _ in range(20):
        encrypted = tv.encrypt_for_user("u1", "google", "same-plaintext")
        assert encrypted.ciphertext not in seen
        seen.add(encrypted.ciphertext)


def test_salt_lives_inside_envelope_not_in_dataclass() -> None:
    """The per-row salt must live INSIDE the Fernet ciphertext (so it
    is auth-protected), not as a separate dataclass field.

    AS.0.4 §AS.2.2 schema declares only ``access_token_enc`` +
    ``key_version`` columns — no ``salt`` column. If a future refactor
    adds a public ``salt`` attribute it needs to update the schema and
    AS.0.4 §3 simultaneously, which is exactly what this test would
    flag."""
    encrypted = tv.encrypt_for_user("u1", "google", "tok123")
    assert set(vars(encrypted).keys()) == {"ciphertext", "key_version"}


def test_envelope_carries_salt_field() -> None:
    """Decrypt the ciphertext directly via secret_store (peek inside
    the envelope) — the JSON MUST carry a base64 salt and the binding
    fields. Pins the envelope shape so AS.2.3 TS twin can mirror it.
    """
    encrypted = tv.encrypt_for_user("user-peek", "google", "tok-peek")
    raw = secret_store.decrypt(encrypted.ciphertext)
    envelope = json.loads(raw)
    assert envelope["fmt"] == tv.BINDING_FORMAT_VERSION
    assert envelope["uid"] == "user-peek"
    assert envelope["prv"] == "google"
    assert envelope["tok"] == "tok-peek"
    assert isinstance(envelope["salt"], str) and len(envelope["salt"]) >= 16


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — provider whitelist + cross-module drift guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_unsupported_provider_rejected_on_encrypt() -> None:
    with pytest.raises(tv.UnsupportedProviderError):
        tv.encrypt_for_user("u1", "facebook", "tok")


def test_unsupported_provider_rejected_on_decrypt() -> None:
    encrypted = tv.encrypt_for_user("u1", "google", "tok")
    with pytest.raises(tv.UnsupportedProviderError):
        tv.decrypt_for_user("u1", "facebook", encrypted)


def test_empty_provider_rejected() -> None:
    with pytest.raises(tv.UnsupportedProviderError):
        tv.encrypt_for_user("u1", "", "tok")


def test_unsupported_provider_error_subclasses_value_error() -> None:
    """Existing call sites that catch ``ValueError`` for provider input
    validation must keep working."""
    assert issubclass(tv.UnsupportedProviderError, ValueError)


def test_supported_providers_aligned_with_account_linking() -> None:
    """AS.0.4 §5.2 hard invariant: vault whitelist == account_linking
    whitelist == AS.1 vendor catalog (4-of-11 subset).

    Adding a 5th OAuth provider requires touching both modules in the
    same PR — this drift guard fires red until they re-converge.
    """
    assert tv.SUPPORTED_PROVIDERS == account_linking._AS1_OAUTH_PROVIDERS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 4 — binding mismatch (anti-shuffle)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_binding_mismatch_on_user_id_swap() -> None:
    encrypted = tv.encrypt_for_user("user-a", "google", "tok-a")
    with pytest.raises(tv.BindingMismatchError):
        tv.decrypt_for_user("user-b", "google", encrypted)


def test_binding_mismatch_on_provider_swap() -> None:
    """Same user, different provider — ciphertext was bound to (user, google)
    but caller is asking us to treat it as (user, github)."""
    encrypted = tv.encrypt_for_user("user-a", "google", "tok-a")
    with pytest.raises(tv.BindingMismatchError):
        tv.decrypt_for_user("user-a", "github", encrypted)


def test_binding_mismatch_swapped_rows() -> None:
    """Concrete attack scenario: attacker swaps user-A's encrypted_access_token
    column value into user-B's row. Decrypt of user-B's row must fail
    even though the ciphertext is valid Fernet."""
    a_token = tv.encrypt_for_user("user-a", "google", "secret-a")
    b_token = tv.encrypt_for_user("user-b", "google", "secret-b")
    # Pretend attacker shuffled a's ciphertext into b's row.
    swapped = tv.EncryptedToken(
        ciphertext=a_token.ciphertext,
        key_version=b_token.key_version,
    )
    with pytest.raises(tv.BindingMismatchError):
        tv.decrypt_for_user("user-b", "google", swapped)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 5 — key_version reservation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_key_version_current_is_one() -> None:
    """AS.0.4 §3.1: this release ships with key_version=1; the column
    is reserved for future KMS rotation."""
    assert tv.KEY_VERSION_CURRENT == 1


def test_unknown_key_version_rejected() -> None:
    encrypted = tv.encrypt_for_user("u1", "google", "tok")
    fake = tv.EncryptedToken(ciphertext=encrypted.ciphertext, key_version=2)
    with pytest.raises(tv.UnknownKeyVersionError):
        tv.decrypt_for_user("u1", "google", fake)


def test_zero_key_version_rejected() -> None:
    encrypted = tv.encrypt_for_user("u1", "google", "tok")
    fake = tv.EncryptedToken(ciphertext=encrypted.ciphertext, key_version=0)
    with pytest.raises(tv.UnknownKeyVersionError):
        tv.decrypt_for_user("u1", "google", fake)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 6 — ciphertext corruption
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_garbage_ciphertext_raises_corrupted() -> None:
    fake = tv.EncryptedToken(ciphertext="not-a-real-fernet-token", key_version=1)
    with pytest.raises(tv.CiphertextCorruptedError):
        tv.decrypt_for_user("u1", "google", fake)


def test_tampered_ciphertext_raises_corrupted() -> None:
    """Flipping any byte in a Fernet token breaks the HMAC; the vault
    surfaces this as :class:`CiphertextCorruptedError`."""
    encrypted = tv.encrypt_for_user("u1", "google", "tok")
    tampered = encrypted.ciphertext[:-2] + ("AA" if encrypted.ciphertext[-2:] != "AA" else "BB")
    fake = tv.EncryptedToken(ciphertext=tampered, key_version=1)
    with pytest.raises(tv.CiphertextCorruptedError):
        tv.decrypt_for_user("u1", "google", fake)


def test_envelope_with_unknown_format_raises_binding_mismatch() -> None:
    """If the inner envelope JSON parses but its ``fmt`` is unknown,
    treat it as a binding-version mismatch (Fernet auth already passed,
    so the ciphertext itself is intact — it just speaks a different
    dialect)."""
    bogus = json.dumps({"fmt": 99, "uid": "u1", "prv": "google", "tok": "x", "salt": "AA=="})
    ciphertext = secret_store.encrypt(bogus)
    fake = tv.EncryptedToken(ciphertext=ciphertext, key_version=1)
    with pytest.raises(tv.BindingMismatchError):
        tv.decrypt_for_user("u1", "google", fake)


def test_envelope_missing_uid_raises_corrupted() -> None:
    bogus = json.dumps({"fmt": 1, "prv": "google", "tok": "x", "salt": "AA=="})
    ciphertext = secret_store.encrypt(bogus)
    fake = tv.EncryptedToken(ciphertext=ciphertext, key_version=1)
    with pytest.raises(tv.CiphertextCorruptedError):
        tv.decrypt_for_user("u1", "google", fake)


def test_non_dict_envelope_raises_corrupted() -> None:
    ciphertext = secret_store.encrypt(json.dumps(["just", "a", "list"]))
    fake = tv.EncryptedToken(ciphertext=ciphertext, key_version=1)
    with pytest.raises(tv.CiphertextCorruptedError):
        tv.decrypt_for_user("u1", "google", fake)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 7 — input validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_empty_plaintext_rejected() -> None:
    with pytest.raises(tv.TokenVaultError):
        tv.encrypt_for_user("u1", "google", "")


def test_non_string_plaintext_rejected() -> None:
    with pytest.raises(tv.TokenVaultError):
        tv.encrypt_for_user("u1", "google", 12345)  # type: ignore[arg-type]


def test_empty_user_id_rejected() -> None:
    with pytest.raises(tv.TokenVaultError):
        tv.encrypt_for_user("", "google", "tok")


def test_decrypt_rejects_non_encrypted_token_input() -> None:
    with pytest.raises(tv.TokenVaultError):
        tv.decrypt_for_user("u1", "google", "raw-string-not-a-dataclass")  # type: ignore[arg-type]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 8 — fingerprint re-export
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_fingerprint_matches_secret_store() -> None:
    """`tv.fingerprint` is a thin re-export — must agree with
    :func:`secret_store.fingerprint` byte-for-byte so vault callers can
    redact tokens for logs/UI without importing both."""
    samples = ["", "abc", "ghp_abc123def456ghi789jkl"]
    for s in samples:
        assert tv.fingerprint(s) == secret_store.fingerprint(s or "")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 9 — single-master-key invariant (AS.0.4 §5.4 grep guard)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_token_vault_uses_secret_store_fernet() -> None:
    """AS.0.4 §3 hard invariant: vault MUST go through
    ``backend.secret_store`` (single master Fernet key) and MUST NOT
    introduce a second master key.

    Mirrors the canonical drift guard in AS.0.4 §5.4 line 240,
    adapted to the actual module path (``backend.security.token_vault``
    per the AS.1.1 / AS.1.3 / AS.1.4 path-deviation precedent).
    """
    src = inspect.getsource(tv)
    # MUST go through secret_store
    assert (
        "from backend import secret_store" in src
        or "from backend.secret_store" in src
    ), "vault must reuse backend.secret_store master key"
    # MUST NOT mint its own Fernet key
    assert "Fernet.generate_key" not in src, (
        "vault must not introduce a second master key — see AS.0.4 §3"
    )
    # MUST NOT introduce a second env var
    assert "OMNISIGHT_OAUTH_SECRET_KEY" not in src, (
        "vault must not introduce OMNISIGHT_OAUTH_SECRET_KEY — see AS.0.4 §3"
    )
    # No HKDF / KDF call: vault must not derive a per-row sub-key
    for forbidden in ("HKDF", "PBKDF2", "Scrypt", "derive_key"):
        assert forbidden not in src, (
            f"vault must not derive sub-keys ({forbidden!r}) — see AS.0.4 §3.1"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 10 — module-global state audit (SOP §1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_no_module_level_mutable_state() -> None:
    """SOP §1: only frozen dataclasses, immutable tuples / strings, and
    frozensets are allowed at module scope."""
    forbidden = (list, dict, set)
    for name, value in vars(tv).items():
        if name.startswith("_"):
            continue
        if callable(value):
            continue
        if isinstance(value, type):  # exception classes / dataclass types
            continue
        assert not isinstance(value, forbidden), (
            f"module-level {name} is mutable {type(value).__name__} — "
            f"SOP §1 forbids cross-worker drift surfaces"
        )


def test_constants_stable_across_reload() -> None:
    """``importlib.reload`` must not change the public constants.
    Catches accidental introduction of import-time randomness in module
    body (e.g. someone moves :func:`secrets.token_bytes` out of a
    function and into a module-level call)."""
    before = (
        tv.KEY_VERSION_CURRENT,
        tv.BINDING_FORMAT_VERSION,
        frozenset(tv.SUPPORTED_PROVIDERS),
    )
    reloaded = importlib.reload(tv)
    after = (
        reloaded.KEY_VERSION_CURRENT,
        reloaded.BINDING_FORMAT_VERSION,
        frozenset(reloaded.SUPPORTED_PROVIDERS),
    )
    assert before == after


def test_import_secrets_provenance() -> None:
    """SOP §1 cross-worker audit answer #1 (deterministic-by-construction):
    randomness MUST come from :mod:`secrets` (kernel CSPRNG), not
    :mod:`random` (Mersenne Twister). Mirrors AS.1.1 / AS.0.10 grep."""
    src = inspect.getsource(tv)
    assert "import secrets" in src, "vault must use secrets module for salts"
    assert "import random" not in src, (
        "vault must not import random — use secrets for cryptographic randomness"
    )


def test_public_surface_matches_all() -> None:
    """``__all__`` lists every public name the vault wants exported."""
    expected = {
        "BINDING_FORMAT_VERSION",
        "BindingMismatchError",
        "CiphertextCorruptedError",
        "EncryptedToken",
        "KEY_VERSION_CURRENT",
        "SUPPORTED_PROVIDERS",
        "TokenVaultError",
        "UnknownKeyVersionError",
        "UnsupportedProviderError",
        "decrypt_for_user",
        "encrypt_for_user",
        "fingerprint",
        "is_enabled",
    }
    assert set(tv.__all__) == expected
    for name in expected:
        assert hasattr(tv, name), f"{name} declared in __all__ but missing"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 11 — AS.0.8 single-knob hook (forward-promotion guard)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_is_enabled_returns_bool() -> None:
    """:func:`is_enabled` MUST return a real bool regardless of the
    underlying ``settings.as_enabled`` shape (forward-promotion safe).
    Mirrors AS.1.1's same-named hook test."""
    assert isinstance(tv.is_enabled(), bool)


def test_is_enabled_default_true_when_setting_absent() -> None:
    """Until AS.3.1 lands the field on :class:`Settings`,
    :func:`is_enabled` defaults to True so the vault stays usable."""
    try:
        from backend.config import settings
    except Exception:
        pytest.skip("config module unavailable")
    if hasattr(settings, "as_enabled"):
        # AS.3.1 has landed; assert the live value, not the fallback.
        assert tv.is_enabled() == bool(settings.as_enabled)
    else:
        assert tv.is_enabled() is True


def test_pure_helpers_callable_when_knob_off() -> None:
    """AS.0.4 §6.2 / module docstring: the pure encrypt / decrypt
    helpers must keep working even when the feature flag is off, so
    backfill / DSAR / key-rotation scripts run regardless. Caller
    endpoints (AS.6.x) gate on :func:`is_enabled` before reaching the
    vault, not the vault itself."""
    encrypted = tv.encrypt_for_user("u1", "google", "tok")
    assert tv.decrypt_for_user("u1", "google", encrypted) == "tok"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 12 — AS.2.3 TS-twin path reservation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_ts_twin_path_reserved_in_docstring() -> None:
    """AS.2.3 will land the libsodium-equivalent TS twin under
    ``templates/_shared/token-vault/``. This test pins the future path
    in the module docstring so AS.2.3 PR knows where to land — and so
    a future contributor doesn't accidentally place the twin somewhere
    inconsistent with the AS.1.x sibling pattern."""
    doc = tv.__doc__ or ""
    assert "templates/_shared/token-vault/" in doc
    # AS.2.3 row is forward-reserved in the AS.0.4 plan + this docstring
    assert "AS.2.3" in doc


def test_ts_twin_directory_present() -> None:
    """The TS twin landed in AS.2.3 — assert the canonical path
    exists with the expected ``index.ts`` + ``README.md`` artefacts.

    Behavioural Python ↔ TS drift is enforced separately by
    :mod:`backend.tests.test_token_vault_shape_drift` (AS.1.5-style
    cross-twin parity matrix, gated on Node ≥22)."""
    twin_dir = (
        pathlib.Path(__file__).resolve().parent.parent.parent
        / "templates"
        / "_shared"
        / "token-vault"
    )
    assert twin_dir.exists(), f"AS.2.3 TS twin directory missing at {twin_dir}"
    assert (twin_dir / "index.ts").exists(), "AS.2.3 TS twin index.ts missing"
    assert (twin_dir / "README.md").exists(), "AS.2.3 TS twin README.md missing"

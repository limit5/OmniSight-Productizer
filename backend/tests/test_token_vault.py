"""AS.2.1 — `backend.security.token_vault` contract tests.

Validates the OAuth token-vault primitive that AS.2.2 (alembic 0057
``oauth_tokens``) and AS.6.x (OAuth router endpoints) will round-trip
through. Pinned invariants:

1. Round-trip — encrypt(uid, prv, tok) → decrypt(uid, prv, …) returns
   the exact plaintext.
2. Per-row salt — same plaintext encrypts to a different ciphertext on
   each call (binding-envelope salt + Fernet IV both contribute).
3. Provider whitelist — only the supported AS.1 providers are accepted, and
   the whitelist byte-equals ``account_linking._AS1_OAUTH_PROVIDERS``
   (AS.0.4 §5.2 cross-module drift guard).
4. Binding mismatch — decrypt with the wrong user_id / wrong provider
   raises :class:`BindingMismatchError`. Defends against DB-level row
   shuffles.
5. Key version — :data:`KEY_VERSION_CURRENT` is the only accepted
   version on the landing date; future quarters advance via
   :func:`current_key_version` and old rows return a lazy re-encrypt
   replacement (AS.0.4 §3.1 / KS.1.4).
6. Ciphertext corruption — Fernet auth failures + malformed inner
   envelopes are translated to :class:`CiphertextCorruptedError`.
7. KS.1.3 envelope invariant — module source MUST go through
   ``backend.security.envelope`` for new writes and MUST NOT mention
   ``Fernet.generate_key`` or ``OMNISIGHT_OAUTH_SECRET_KEY``.
8. Module-global state audit — per SOP §1: no module-level mutable
   state, constants stable across :func:`importlib.reload`, no IO at
   import time, ``import secrets`` provenance grep (mirrors AS.1.1).
9. AS.0.8 single-knob — :func:`is_enabled` reads ``settings.as_enabled``
   via getattr fallback (forward-promotion guard, mirrors AS.0.9 §7.2.6).
10. Empty / wrong-type inputs — vault refuses empty plaintext, missing
    user_id, non-string token wrapper.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import pathlib
import subprocess
import sys

import pytest
from cryptography.fernet import InvalidToken

from backend import account_linking, secret_store
from backend.security import envelope as tenant_envelope
from backend.security import token_vault as tv


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — round-trip
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize(
    "provider",
    [
        "google", "github", "apple", "microsoft", "discord", "gitlab",
        "bitbucket", "slack",
    ],
)
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
    """Decrypt the KS.1.3 token envelope (peek inside the binding
    payload) — the JSON MUST carry a base64 salt and the binding
    fields.
    """
    encrypted = tv.encrypt_for_user("user-peek", "google", "tok-peek")
    outer = json.loads(encrypted.ciphertext)
    raw = tenant_envelope.decrypt(
        outer["ciphertext"],
        tenant_envelope.TenantDEKRef.from_dict(outer["dek_ref"]),
    )
    envelope = json.loads(raw)
    assert envelope["fmt"] == tv.BINDING_FORMAT_VERSION
    assert envelope["uid"] == "user-peek"
    assert envelope["prv"] == "google"
    assert envelope["tok"] == "tok-peek"
    assert isinstance(envelope["salt"], str) and len(envelope["salt"]) >= 16


def test_token_envelope_shape_is_pinned() -> None:
    """KS.1.3 completion writes only the new token envelope."""
    encrypted = tv.encrypt_for_user("user-peek", "google", "tok-peek")
    outer = json.loads(encrypted.ciphertext)
    assert encrypted.key_version == tv.KEY_VERSION_CURRENT
    assert outer["fmt"] == tv.TOKEN_ENVELOPE_FORMAT_VERSION
    assert isinstance(outer["ciphertext"], str) and outer["ciphertext"].startswith("{")
    assert set(outer["dek_ref"]) == {
        "dek_id",
        "encryption_context",
        "key_id",
        "key_version",
        "provider",
        "schema_version",
        "tenant_id",
        "wrap_algorithm",
        "wrapped_dek_b64",
    }
    assert outer["dek_ref"]["tenant_id"] == "user-peek"
    assert outer["dek_ref"]["encryption_context"]["purpose"] == "as-token-vault"


def test_default_write_is_not_single_fernet_ciphertext() -> None:
    """KS.1 Phase 1: AS Token Vault must not regress to raw
    ``secret_store`` Fernet for default writes."""
    encrypted = tv.encrypt_for_user(
        "user-envelope-only",
        "github",
        "ghp_envelope_only",
    )

    with pytest.raises(InvalidToken):
        secret_store.decrypt(encrypted.ciphertext)

    outer = json.loads(encrypted.ciphertext)
    assert outer["fmt"] == tv.TOKEN_ENVELOPE_FORMAT_VERSION
    assert (
        tv.decrypt_for_user("user-envelope-only", "github", encrypted)
        == "ghp_envelope_only"
    )


def test_envelope_disabled_env_no_longer_writes_legacy_fernet_token(monkeypatch) -> None:
    """KS.1 completion: the old rollback env no longer produces
    single-Fernet OAuth token rows."""
    monkeypatch.setenv(tenant_envelope.ENVELOPE_ENABLED_ENV, "false")
    encrypted = tv.encrypt_for_user("user-rollback", "google", "tok-rollback")

    assert encrypted.key_version == tv.KEY_VERSION_CURRENT
    assert encrypted.ciphertext.lstrip().startswith("{")
    with pytest.raises(InvalidToken):
        secret_store.decrypt(encrypted.ciphertext)
    assert tv.decrypt_for_user("user-rollback", "google", encrypted) == "tok-rollback"


def test_encrypt_accepts_explicit_tenant_id() -> None:
    encrypted = tv.encrypt_for_user(
        "user-peek",
        "google",
        "tok-peek",
        tenant_id="tenant-42",
    )
    outer = json.loads(encrypted.ciphertext)
    assert outer["dek_ref"]["tenant_id"] == "tenant-42"
    assert tv.decrypt_for_user("user-peek", "google", encrypted) == "tok-peek"


def test_oauth_token_envelope_survives_hard_restart(monkeypatch) -> None:
    """KS.1.11 compat regression: an ``oauth_tokens`` ciphertext
    written on the new envelope path must decrypt in a fresh interpreter.

    This simulates the post-contract random hard-restart case:
    no module-global Fernet / KMS adapter cache is shared with the
    reader process; both workers derive the same local KEK from
    ``OMNISIGHT_SECRET_KEY``.
    """
    monkeypatch.setenv(
        "OMNISIGHT_SECRET_KEY",
        "ks-1-11-oauth-hard-restart-secret",
    )
    secret_store._reset_for_tests()
    encrypted = tv.encrypt_for_user(
        "user-restart",
        "github",
        "ghp_restart_token",
        tenant_id="tenant-restart",
    )
    payload = json.dumps({
        "ciphertext": encrypted.ciphertext,
        "key_version": encrypted.key_version,
    })
    code = """
import json
import sys
from backend.security import token_vault as tv

raw = json.loads(sys.stdin.read())
token = tv.EncryptedToken(
    ciphertext=raw["ciphertext"],
    key_version=raw["key_version"],
)
print(tv.decrypt_for_user("user-restart", "github", token))
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        input=payload,
        text=True,
        capture_output=True,
        check=True,
    )
    assert proc.stdout.strip() == "ghp_restart_token"


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
    whitelist == AS.1 vendor catalog subset.

    Adding an OAuth provider requires touching both modules in the
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


def test_key_version_current_stays_one() -> None:
    """KS.1.3 keeps key_version=1 for schema compatibility; new vs
    legacy rows are distinguished by ciphertext envelope shape."""
    assert tv.KEY_VERSION_INITIAL == 1
    assert tv.KEY_VERSION_CURRENT == 1
    assert tv.KEY_VERSION_LEGACY_FERNET == 1
    assert tv.current_key_version(as_of=tv.KEY_VERSION_ROTATION_STARTED_ON) == 1


def test_quarterly_key_version_schedule_advances() -> None:
    """KS.1.4: quarterly automatic master-KEK rotation derives the
    active epoch from UTC date constants, not from process memory."""
    first_day_v2 = (
        tv.KEY_VERSION_ROTATION_STARTED_ON
        + tv._dt.timedelta(days=tv.KEY_VERSION_ROTATION_INTERVAL_DAYS)
    )
    assert tv.current_key_version(as_of=first_day_v2) == 2
    assert tv.current_key_version(
        as_of=first_day_v2 + tv._dt.timedelta(days=tv.KEY_VERSION_ROTATION_INTERVAL_DAYS)
    ) == 3


def test_future_quarter_encrypt_uses_scheduled_key_version() -> None:
    first_day_v2 = (
        tv.KEY_VERSION_ROTATION_STARTED_ON
        + tv._dt.timedelta(days=tv.KEY_VERSION_ROTATION_INTERVAL_DAYS)
    )
    encrypted = tv.encrypt_for_user(
        "u1",
        "google",
        "tok",
        as_of=first_day_v2,
    )
    assert encrypted.key_version == 2
    assert tv.decrypt_for_user("u1", "google", encrypted, as_of=first_day_v2) == "tok"


def test_lazy_reencrypt_returns_replacement_for_old_row() -> None:
    first_day_v2 = (
        tv.KEY_VERSION_ROTATION_STARTED_ON
        + tv._dt.timedelta(days=tv.KEY_VERSION_ROTATION_INTERVAL_DAYS)
    )
    old = tv.encrypt_for_user(
        "u1",
        "google",
        "tok",
        as_of=tv.KEY_VERSION_ROTATION_STARTED_ON,
    )
    result = tv.decrypt_for_user_with_lazy_reencrypt(
        "u1",
        "google",
        old,
        tenant_id="tenant-42",
        as_of=first_day_v2,
    )
    assert result.plaintext == "tok"
    assert result.key_version == 1
    assert result.target_key_version == 2
    assert result.replacement is not None
    assert result.replacement.key_version == 2
    outer = json.loads(result.replacement.ciphertext)
    assert outer["dek_ref"]["tenant_id"] == "tenant-42"
    assert tv.decrypt_for_user(
        "u1",
        "google",
        result.replacement,
        as_of=first_day_v2,
    ) == "tok"


def test_lazy_reencrypt_noops_for_current_row() -> None:
    first_day_v2 = (
        tv.KEY_VERSION_ROTATION_STARTED_ON
        + tv._dt.timedelta(days=tv.KEY_VERSION_ROTATION_INTERVAL_DAYS)
    )
    current = tv.encrypt_for_user(
        "u1",
        "google",
        "tok",
        as_of=first_day_v2,
    )
    result = tv.decrypt_for_user_with_lazy_reencrypt(
        "u1",
        "google",
        current,
        as_of=first_day_v2,
    )
    assert result.plaintext == "tok"
    assert result.replacement is None
    assert tv.key_version_needs_lazy_reencrypt(1, as_of=first_day_v2) is True
    assert tv.key_version_needs_lazy_reencrypt(2, as_of=first_day_v2) is False


def test_unknown_key_version_rejected() -> None:
    encrypted = tv.encrypt_for_user("u1", "google", "tok")
    future_version = tv.current_key_version() + 1
    fake = tv.EncryptedToken(ciphertext=encrypted.ciphertext, key_version=future_version)
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
    """Legacy Fernet binding envelopes are deprecated even when their
    inner JSON would have parsed during the compatibility window."""
    bogus = json.dumps({"fmt": 99, "uid": "u1", "prv": "google", "tok": "x", "salt": "AA=="})
    ciphertext = secret_store.encrypt(bogus)
    fake = tv.EncryptedToken(ciphertext=ciphertext, key_version=1)
    with pytest.raises(tv.LegacyFernetFallbackDeprecatedError):
        tv.decrypt_for_user("u1", "google", fake)


def test_envelope_missing_uid_raises_corrupted() -> None:
    bogus = json.dumps({"fmt": 1, "prv": "google", "tok": "x", "salt": "AA=="})
    ciphertext = secret_store.encrypt(bogus)
    fake = tv.EncryptedToken(ciphertext=ciphertext, key_version=1)
    with pytest.raises(tv.LegacyFernetFallbackDeprecatedError):
        tv.decrypt_for_user("u1", "google", fake)


def test_non_dict_envelope_raises_corrupted() -> None:
    ciphertext = secret_store.encrypt(json.dumps(["just", "a", "list"]))
    fake = tv.EncryptedToken(ciphertext=ciphertext, key_version=tv.KEY_VERSION_LEGACY_FERNET)
    with pytest.raises(tv.LegacyFernetFallbackDeprecatedError):
        tv.decrypt_for_user("u1", "google", fake)


def test_legacy_fernet_fallback_rejects_old_rows() -> None:
    """KS.1 completion: key_version=1 old Fernet rows no longer
    decrypt after the migration/backfill window."""
    legacy_payload = json.dumps(
        {"fmt": 1, "uid": "u1", "prv": "google", "tok": "legacy", "salt": "AA=="},
        sort_keys=True,
        separators=(",", ":"),
    )
    token = tv.EncryptedToken(
        ciphertext=secret_store.encrypt(legacy_payload),
        key_version=tv.KEY_VERSION_LEGACY_FERNET,
    )
    with pytest.raises(tv.LegacyFernetFallbackDeprecatedError):
        tv.decrypt_for_user("u1", "google", token)


def test_legacy_fernet_fallback_is_inactive_after_completion() -> None:
    legacy_payload = json.dumps(
        {"fmt": 1, "uid": "u1", "prv": "google", "tok": "legacy", "salt": "AA=="},
        sort_keys=True,
        separators=(",", ":"),
    )
    token = tv.EncryptedToken(
        ciphertext=secret_store.encrypt(legacy_payload),
        key_version=tv.KEY_VERSION_LEGACY_FERNET,
    )
    assert tv.legacy_fernet_fallback_is_active(
        as_of=tv.LEGACY_FERNET_FALLBACK_DEPRECATES_ON
    ) is False
    with pytest.raises(tv.LegacyFernetFallbackDeprecatedError):
        tv.decrypt_for_user("u1", "google", token)


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
#  Family 9 — KS.1.3 envelope invariant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_token_vault_uses_ks_envelope_for_new_writes() -> None:
    """KS.1 completion invariant: writes MUST go through
    ``backend.security.envelope``; legacy Fernet writer fallback is
    deprecated."""
    src = inspect.getsource(tv)
    assert "from backend.security import envelope as tenant_envelope" in src
    assert "tenant_envelope.encrypt" in src
    assert "tenant_envelope.is_enabled()" not in src, (
        "KS.1 completion must not retain the single-Fernet rollback writer"
    )
    assert "os.environ" not in src, (
        "token_vault must not parse the KS knob itself"
    )
    # MUST NOT mint its own Fernet key
    assert "Fernet.generate_key" not in src, (
        "vault must not introduce a second Fernet master key"
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
    """Loading a fresh module copy must not change public constants.
    Catches accidental introduction of import-time randomness in module
    body without leaving a reloaded module object behind for tests that
    imported ``TokenVaultError`` directly."""
    before = (
        tv.KEY_VERSION_CURRENT,
        tv.KEY_VERSION_LEGACY_FERNET,
        tv.KEY_VERSION_ROTATION_INTERVAL_DAYS,
        tv.KEY_VERSION_ROTATION_STARTED_ON,
        tv.BINDING_FORMAT_VERSION,
        tv.TOKEN_ENVELOPE_FORMAT_VERSION,
        tv.LEGACY_FERNET_FALLBACK_STARTED_ON,
        tv.LEGACY_FERNET_FALLBACK_DEPRECATES_ON,
        frozenset(tv.SUPPORTED_PROVIDERS),
    )
    spec = importlib.util.spec_from_file_location(
        "_token_vault_reload_probe",
        pathlib.Path(tv.__file__ or ""),
    )
    assert spec is not None and spec.loader is not None
    reloaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reloaded
    try:
        spec.loader.exec_module(reloaded)
    finally:
        sys.modules.pop(spec.name, None)
    after = (
        reloaded.KEY_VERSION_CURRENT,
        reloaded.KEY_VERSION_LEGACY_FERNET,
        reloaded.KEY_VERSION_ROTATION_INTERVAL_DAYS,
        reloaded.KEY_VERSION_ROTATION_STARTED_ON,
        reloaded.BINDING_FORMAT_VERSION,
        reloaded.TOKEN_ENVELOPE_FORMAT_VERSION,
        reloaded.LEGACY_FERNET_FALLBACK_STARTED_ON,
        reloaded.LEGACY_FERNET_FALLBACK_DEPRECATES_ON,
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
        "DecryptedToken",
        "EncryptedToken",
        "KEY_VERSION_INITIAL",
        "KEY_VERSION_CURRENT",
        "KEY_VERSION_LEGACY_FERNET",
        "KEY_VERSION_ROTATION_INTERVAL_DAYS",
        "KEY_VERSION_ROTATION_STARTED_ON",
        "LEGACY_FERNET_FALLBACK_DEPRECATES_ON",
        "LEGACY_FERNET_FALLBACK_STARTED_ON",
        "LegacyFernetFallbackDeprecatedError",
        "SUPPORTED_PROVIDERS",
        "TOKEN_ENVELOPE_FORMAT_VERSION",
        "TokenVaultError",
        "UnknownKeyVersionError",
        "UnknownTokenEnvelopeVersionError",
        "UnsupportedProviderError",
        "current_key_version",
        "decrypt_for_user",
        "decrypt_for_user_with_audit",
        "decrypt_for_user_with_lazy_reencrypt",
        "encrypt_for_user",
        "fingerprint",
        "is_enabled",
        "key_version_needs_lazy_reencrypt",
        "legacy_fernet_fallback_is_active",
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


@pytest.mark.asyncio
async def test_decrypt_for_user_with_audit_emits_key_metadata(monkeypatch) -> None:
    """KS.1.5: audited decrypt writes tenant / user / key_id /
    request_id metadata and never includes plaintext in the audit body."""
    encrypted = tv.encrypt_for_user(
        "u1",
        "google",
        "tok-secret",
        tenant_id="t-ks15",
    )
    captured = {}

    async def fake_emit(ctx):
        captured["ctx"] = ctx
        return 15

    monkeypatch.setattr(
        "backend.security.token_vault.decryption_audit.emit_decryption",
        fake_emit,
    )
    plaintext = await tv.decrypt_for_user_with_audit(
        "u1",
        "google",
        encrypted,
        request_id="req-ks15",
        actor="alice@example.com",
    )

    assert plaintext == "tok-secret"
    ctx = captured["ctx"]
    assert ctx.tenant_id == "t-ks15"
    assert ctx.user_id == "u1"
    assert ctx.request_id == "req-ks15"
    assert ctx.key_id == "local-fernet"
    assert ctx.provider == "local-fernet"
    assert ctx.purpose == "as-token-vault"
    assert ctx.actor == "alice@example.com"
    assert "tok-secret" not in repr(ctx)


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

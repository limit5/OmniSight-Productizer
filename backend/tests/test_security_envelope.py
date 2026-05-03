"""KS.1.2 -- per-tenant DEK envelope helper contract tests."""

from __future__ import annotations

import base64
import inspect
import json

import pytest

from backend import secret_store
from backend.security import envelope
from backend.security import kms_adapters as kms


TENANT_A = "t-acme"
TENANT_B = "t-beta"


def test_round_trip_local_fernet_kms_adapter(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-2-round-trip")
    secret_store._reset_for_tests()

    ciphertext, dek_ref = envelope.encrypt("sk-ant-secret", TENANT_A)

    assert isinstance(dek_ref, envelope.TenantDEKRef)
    assert dek_ref.tenant_id == TENANT_A
    assert dek_ref.provider == "local-fernet"
    assert dek_ref.schema_version == envelope.KEY_VERSION_CURRENT
    assert ciphertext != "sk-ant-secret"
    assert envelope.decrypt(ciphertext, dek_ref) == "sk-ant-secret"


def test_round_trip_long_unicode_plaintext(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-2-unicode")
    secret_store._reset_for_tests()
    plaintext = "token-" + "𓀀𓁀𓂀𓃀" * 20 + "-tail"

    ciphertext, dek_ref = envelope.encrypt(plaintext, TENANT_A)

    assert envelope.decrypt(ciphertext, dek_ref) == plaintext


def test_ciphertext_envelope_shape_is_pinned(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-2-shape")
    secret_store._reset_for_tests()

    ciphertext, dek_ref = envelope.encrypt("payload", TENANT_A)
    payload = json.loads(ciphertext)

    assert payload["fmt"] == envelope.ENVELOPE_FORMAT_VERSION
    assert payload["alg"] == envelope.AES_GCM_ALGORITHM
    assert payload["dek"] == dek_ref.dek_id
    assert payload["tid"] == TENANT_A
    assert set(payload) == {
        "alg",
        "ciphertext_b64",
        "dek",
        "fmt",
        "nonce_b64",
        "tid",
    }
    assert len(base64.b64decode(payload["nonce_b64"])) == envelope.NONCE_RAW_BYTES


def test_tenant_dek_ref_schema_round_trips(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-2-dek-ref")
    secret_store._reset_for_tests()

    ciphertext, dek_ref = envelope.encrypt("payload", TENANT_A)
    restored = envelope.TenantDEKRef.from_dict(dek_ref.to_dict())

    assert restored == dek_ref
    assert envelope.decrypt(ciphertext, restored) == "payload"
    assert set(restored.to_dict()) == {
        "dek_id",
        "tenant_id",
        "provider",
        "key_id",
        "wrapped_dek_b64",
        "key_version",
        "wrap_algorithm",
        "encryption_context",
        "schema_version",
    }


def test_same_plaintext_generates_distinct_deks_and_ciphertexts(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-2-randomness")
    secret_store._reset_for_tests()

    seen_ciphertexts: set[str] = set()
    seen_deks: set[str] = set()
    for _ in range(20):
        ciphertext, dek_ref = envelope.encrypt("same-plaintext", TENANT_A)
        assert ciphertext not in seen_ciphertexts
        assert dek_ref.dek_id not in seen_deks
        seen_ciphertexts.add(ciphertext)
        seen_deks.add(dek_ref.dek_id)


def test_tenant_binding_rejects_swapped_dek_ref(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-2-swap")
    secret_store._reset_for_tests()

    ciphertext, _ = envelope.encrypt("tenant-a-payload", TENANT_A)
    _, beta_ref = envelope.encrypt("tenant-b-payload", TENANT_B)

    with pytest.raises(envelope.BindingMismatchError, match="dek_id"):
        envelope.decrypt(ciphertext, beta_ref)


def test_tenant_binding_rejects_ciphertext_tid_tamper(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-2-tamper-tid")
    secret_store._reset_for_tests()

    ciphertext, dek_ref = envelope.encrypt("payload", TENANT_A)
    payload = json.loads(ciphertext)
    payload["tid"] = TENANT_B

    with pytest.raises(envelope.BindingMismatchError, match="tenant_id"):
        envelope.decrypt(json.dumps(payload), dek_ref)


def test_aes_gcm_auth_rejects_ciphertext_tamper(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-2-tamper-ct")
    secret_store._reset_for_tests()

    ciphertext, dek_ref = envelope.encrypt("payload", TENANT_A)
    payload = json.loads(ciphertext)
    raw = bytearray(base64.b64decode(payload["ciphertext_b64"]))
    raw[-1] ^= 1
    payload["ciphertext_b64"] = base64.b64encode(bytes(raw)).decode("ascii")

    with pytest.raises(envelope.CiphertextCorruptedError):
        envelope.decrypt(json.dumps(payload), dek_ref)


def test_wrong_adapter_provider_rejected(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-2-wrong-provider")
    secret_store._reset_for_tests()
    ciphertext, dek_ref = envelope.encrypt("payload", TENANT_A)

    with pytest.raises(envelope.BindingMismatchError, match="does not match"):
        envelope.decrypt(
            ciphertext,
            dek_ref,
            kms_adapter=kms.AWSKMSAdapter(key_id="arn:aws:kms:demo"),
        )


class FakeKMSAdapter:
    provider = "fake-kms"

    def __init__(self) -> None:
        self.wrap_calls = []
        self.unwrap_calls = []

    def wrap_dek(self, plaintext_dek, *, encryption_context=None):
        self.wrap_calls.append((plaintext_dek, encryption_context))
        return kms.WrappedDEK(
            provider=self.provider,
            key_id="fake-key",
            ciphertext=b"wrapped:" + plaintext_dek,
            key_version="v3",
            algorithm="fake-wrap",
            encryption_context=dict(encryption_context or {}),
        )

    def unwrap_dek(self, wrapped_dek, *, encryption_context=None):
        self.unwrap_calls.append((wrapped_dek, encryption_context))
        return wrapped_dek.ciphertext.removeprefix(b"wrapped:")


def test_custom_kms_adapter_receives_tenant_dek_context() -> None:
    adapter = FakeKMSAdapter()

    ciphertext, dek_ref = envelope.encrypt(
        "payload",
        TENANT_A,
        kms_adapter=adapter,
        purpose="oauth-token",
    )

    assert dek_ref.provider == "fake-kms"
    assert dek_ref.key_version == "v3"
    assert dek_ref.wrap_algorithm == "fake-wrap"
    assert adapter.wrap_calls[0][1] == {
        "tenant_id": TENANT_A,
        "dek_id": dek_ref.dek_id,
        "purpose": "oauth-token",
        "schema": "ks.1.2",
    }
    assert envelope.decrypt(ciphertext, dek_ref, kms_adapter=adapter) == "payload"
    assert adapter.unwrap_calls[0][1] == dek_ref.encryption_context


def test_zeroize_bytearray_uses_ctypes_memset() -> None:
    buffer = bytearray(b"secret")

    envelope._zeroize_bytearray(buffer)

    assert buffer == b"\x00" * 6


def test_encrypt_and_decrypt_zeroize_secret_buffers(monkeypatch) -> None:
    calls: list[int] = []
    original_memset = envelope.ctypes.memset

    def recording_memset(ptr, value, size):
        calls.append(size)
        return original_memset(ptr, value, size)

    monkeypatch.setattr(envelope.ctypes, "memset", recording_memset)
    plaintext = "sk-ant-zeroize"

    ciphertext, dek_ref = envelope.encrypt(plaintext, TENANT_A)
    assert envelope.decrypt(ciphertext, dek_ref) == plaintext

    assert envelope.DEK_RAW_BYTES in calls
    assert envelope.NONCE_RAW_BYTES in calls
    assert len(plaintext.encode("utf-8")) in calls


def test_non_local_decrypt_requires_adapter() -> None:
    adapter = FakeKMSAdapter()
    ciphertext, dek_ref = envelope.encrypt("payload", TENANT_A, kms_adapter=adapter)

    with pytest.raises(envelope.EnvelopeEncryptionError, match="kms_adapter is required"):
        envelope.decrypt(ciphertext, dek_ref)


def test_input_validation() -> None:
    with pytest.raises(envelope.EnvelopeEncryptionError, match="plaintext"):
        envelope.encrypt("", TENANT_A)
    with pytest.raises(envelope.EnvelopeEncryptionError, match="tenant_id"):
        envelope.encrypt("payload", "")
    with pytest.raises(envelope.EnvelopeEncryptionError, match="dek_ref"):
        envelope.decrypt("{}", object())  # type: ignore[arg-type]


def test_unknown_versions_rejected(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-2-version")
    secret_store._reset_for_tests()
    ciphertext, dek_ref = envelope.encrypt("payload", TENANT_A)

    payload = json.loads(ciphertext)
    payload["fmt"] = 99
    with pytest.raises(envelope.UnknownEnvelopeVersionError):
        envelope.decrypt(json.dumps(payload), dek_ref)

    data = dek_ref.to_dict()
    data["schema_version"] = 99
    with pytest.raises(envelope.UnknownEnvelopeVersionError):
        envelope.TenantDEKRef.from_dict(data)


def test_envelope_enabled_knob_defaults_true(monkeypatch) -> None:
    monkeypatch.delenv(envelope.ENVELOPE_ENABLED_ENV, raising=False)
    assert envelope.is_enabled() is True


@pytest.mark.parametrize("raw", ["false", "FALSE", "0", "no", "off"])
def test_envelope_enabled_knob_false_values(monkeypatch, raw: str) -> None:
    monkeypatch.setenv(envelope.ENVELOPE_ENABLED_ENV, raw)
    assert envelope.is_enabled() is False


@pytest.mark.parametrize("raw", ["true", "1", "yes", "anything-else"])
def test_envelope_enabled_knob_true_values(monkeypatch, raw: str) -> None:
    monkeypatch.setenv(envelope.ENVELOPE_ENABLED_ENV, raw)
    assert envelope.is_enabled() is True


def test_module_global_state_and_crypto_source_guards() -> None:
    source = inspect.getsource(envelope)
    assert "AESGCM" in source
    assert "ctypes.memset" in source
    assert "secrets.token_bytes" in source
    assert "Fernet.generate_key" not in source
    assert "_adapter_registry" not in source
    assert "_dek_cache" not in source
    assert "secret_store.encrypt" not in source
    assert envelope.__all__ == sorted(envelope.__all__)

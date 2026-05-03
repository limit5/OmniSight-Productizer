"""KS.1.2 -- per-tenant DEK schema and envelope encryption helpers.

This module is the pure helper layer that sits on top of
:mod:`backend.security.kms_adapters`. It owns the serialisable
``TenantDEKRef`` shape and the direct
``encrypt(plaintext, tenant_id) -> (ciphertext, dek_ref)`` /
``decrypt(ciphertext, dek_ref)`` round-trip. Persistence is deliberately
left to KS.1.10's ``tenant_deks`` migration and KS.1.3's token-vault
wiring; no existing caller is changed here.

Cryptographic shape
───────────────────
Each encrypt call mints one 256-bit data-encryption key (DEK), wraps it
through the caller-supplied KMS adapter, then encrypts the plaintext via
AES-256-GCM. The returned ``TenantDEKRef`` is the persistence schema for
that wrapped DEK; the returned ciphertext is a compact JSON envelope::

    {
      "fmt": 1,
      "alg": "AES-256-GCM",
      "dek": "<dek_id>",
      "tid": "<tenant_id>",
      "nonce_b64": "<12 random bytes>",
      "ciphertext_b64": "<AES-GCM ciphertext+tag>"
    }

The tenant id and DEK id are authenticated twice: first in the KMS wrap
``encryption_context`` and again as AES-GCM additional authenticated
data. A row shuffle that pairs ciphertext with another tenant's
``TenantDEKRef`` fails before plaintext is returned.

Memory zeroization (KS.1.9)
───────────────────────────
The helper keeps DEK bytes, nonce bytes, plaintext input bytes, and
decrypted plaintext bytes in ``bytearray`` buffers where possible and
clears those buffers with libsodium ``sodium_memzero`` in ``finally``
blocks after use. This is best-effort only: Python strings, temporary ``bytes``
objects required by cryptography/KMS adapter APIs, and OpenSSL internal
buffers are outside this module's direct control.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
No module-level mutable state. Randomness comes from the kernel CSPRNG
via :mod:`secrets`; KMS client state, if any, is owned by the caller's
adapter instance. The default local adapter derives from
``backend.secret_store``'s file-lock coordinated key source, so every
worker resolves the same local KEK through disk coordination.

Read-after-write timing audit
─────────────────────────────
This row adds no DB writes and no cache. Callers persist the returned
``TenantDEKRef`` and ciphertext atomically in later KS rows, so there is
no read-after-write timing surface in this helper.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from nacl.bindings import sodium_core

from backend.security import kms_adapters


KEY_VERSION_CURRENT: int = 1
ENVELOPE_FORMAT_VERSION: int = 1
AES_GCM_ALGORITHM: str = "AES-256-GCM"
DEK_RAW_BYTES: int = 32
NONCE_RAW_BYTES: int = 12
DEFAULT_PURPOSE: str = "tenant-secret"
ENVELOPE_ENABLED_ENV: str = "OMNISIGHT_KS_ENVELOPE_ENABLED"


class EnvelopeEncryptionError(Exception):
    """Base class for KS.1.2 envelope-encryption errors."""


class BindingMismatchError(EnvelopeEncryptionError):
    """Ciphertext and DEK reference do not belong to the same tenant/DEK."""


class CiphertextCorruptedError(EnvelopeEncryptionError):
    """The ciphertext envelope is malformed or failed AES-GCM auth."""


class UnknownEnvelopeVersionError(EnvelopeEncryptionError):
    """The ciphertext or DEK schema version is not supported."""


@dataclass(frozen=True)
class TenantDEKRef:
    """Persistent schema for one wrapped per-tenant data-encryption key."""

    dek_id: str
    tenant_id: str
    provider: str
    key_id: str
    wrapped_dek_b64: str
    key_version: Optional[str] = None
    wrap_algorithm: str = ""
    encryption_context: dict[str, str] = field(default_factory=dict)
    schema_version: int = KEY_VERSION_CURRENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "dek_id": self.dek_id,
            "tenant_id": self.tenant_id,
            "provider": self.provider,
            "key_id": self.key_id,
            "wrapped_dek_b64": self.wrapped_dek_b64,
            "key_version": self.key_version,
            "wrap_algorithm": self.wrap_algorithm,
            "encryption_context": dict(self.encryption_context),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TenantDEKRef":
        if int(data.get("schema_version", KEY_VERSION_CURRENT)) != KEY_VERSION_CURRENT:
            raise UnknownEnvelopeVersionError(
                f"unknown TenantDEKRef schema_version={data.get('schema_version')!r}"
            )
        return cls(
            dek_id=_require_str(data.get("dek_id"), "dek_id"),
            tenant_id=_require_str(data.get("tenant_id"), "tenant_id"),
            provider=_require_str(data.get("provider"), "provider"),
            key_id=_require_str(data.get("key_id"), "key_id"),
            wrapped_dek_b64=_require_str(data.get("wrapped_dek_b64"), "wrapped_dek_b64"),
            key_version=(
                str(data["key_version"])
                if data.get("key_version") is not None
                else None
            ),
            wrap_algorithm=str(data.get("wrap_algorithm") or ""),
            encryption_context=_context_dict(data.get("encryption_context") or {}),
            schema_version=KEY_VERSION_CURRENT,
        )


def encrypt(
    plaintext: str,
    tenant_id: str,
    *,
    kms_adapter: Optional[kms_adapters.KMSAdapter] = None,
    purpose: str = DEFAULT_PURPOSE,
) -> tuple[str, TenantDEKRef]:
    """Encrypt ``plaintext`` for ``tenant_id`` and return ciphertext + DEK ref."""

    plain = _require_plaintext(plaintext)
    tid = _require_str(tenant_id, "tenant_id")
    adapter = kms_adapter or kms_adapters.LocalFernetKMSAdapter()
    dek_id = _new_dek_id()
    context = _dek_context(tid, dek_id, purpose)
    plaintext_dek = bytearray(secrets.token_bytes(DEK_RAW_BYTES))
    nonce = bytearray(secrets.token_bytes(NONCE_RAW_BYTES))
    plaintext_bytes = bytearray(plain.encode("utf-8"))

    try:
        wrapped = adapter.wrap_dek(
            bytes(plaintext_dek),
            encryption_context=context,
        )
        aesgcm = AESGCM(bytes(plaintext_dek))
        aad = _aad(tid, dek_id)
        encrypted = aesgcm.encrypt(bytes(nonce), bytes(plaintext_bytes), aad)
        nonce_b64 = base64.b64encode(bytes(nonce)).decode("ascii")
    finally:
        _zeroize_bytearray(plaintext_dek)
        _zeroize_bytearray(nonce)
        _zeroize_bytearray(plaintext_bytes)

    dek_ref = TenantDEKRef(
        dek_id=dek_id,
        tenant_id=tid,
        provider=wrapped.provider,
        key_id=wrapped.key_id,
        wrapped_dek_b64=base64.b64encode(wrapped.ciphertext).decode("ascii"),
        key_version=wrapped.key_version,
        wrap_algorithm=wrapped.algorithm,
        encryption_context=dict(wrapped.encryption_context),
    )
    envelope = {
        "fmt": ENVELOPE_FORMAT_VERSION,
        "alg": AES_GCM_ALGORITHM,
        "dek": dek_id,
        "tid": tid,
        "nonce_b64": nonce_b64,
        "ciphertext_b64": base64.b64encode(encrypted).decode("ascii"),
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")), dek_ref


def is_enabled() -> bool:
    """Whether KS.1 envelope writes are enabled.

    The migration rollback knob is read lazily per call, so every
    worker derives the same value from its process environment without
    relying on shared module-global state.
    """

    raw = (os.environ.get(ENVELOPE_ENABLED_ENV) or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def decrypt(
    ciphertext: str,
    dek_ref: TenantDEKRef,
    *,
    kms_adapter: Optional[kms_adapters.KMSAdapter] = None,
) -> str:
    """Decrypt ``ciphertext`` with ``dek_ref`` and return plaintext."""

    if not isinstance(dek_ref, TenantDEKRef):
        raise EnvelopeEncryptionError(
            f"dek_ref must be a TenantDEKRef, got {type(dek_ref).__name__}"
        )
    if dek_ref.schema_version != KEY_VERSION_CURRENT:
        raise UnknownEnvelopeVersionError(
            f"unknown TenantDEKRef schema_version={dek_ref.schema_version!r}"
        )

    envelope = _load_ciphertext_envelope(ciphertext)
    _check_binding(envelope, dek_ref)
    adapter = _adapter_for_ref(dek_ref, kms_adapter)
    wrapped = kms_adapters.WrappedDEK(
        provider=dek_ref.provider,
        key_id=dek_ref.key_id,
        ciphertext=base64.b64decode(dek_ref.wrapped_dek_b64.encode("ascii")),
        key_version=dek_ref.key_version,
        algorithm=dek_ref.wrap_algorithm,
        encryption_context=dict(dek_ref.encryption_context),
    )
    plaintext_dek = bytearray()
    plaintext = bytearray()
    try:
        plaintext_dek = bytearray(
            adapter.unwrap_dek(
                wrapped,
                encryption_context=dek_ref.encryption_context,
            )
        )
        aesgcm = AESGCM(bytes(plaintext_dek))
        plaintext = bytearray(aesgcm.decrypt(
            base64.b64decode(envelope["nonce_b64"].encode("ascii")),
            base64.b64decode(envelope["ciphertext_b64"].encode("ascii")),
            _aad(dek_ref.tenant_id, dek_ref.dek_id),
        ))
        return plaintext.decode("utf-8")
    except kms_adapters.KMSAdapterError:
        raise
    except Exception as exc:
        raise CiphertextCorruptedError(
            "ciphertext failed envelope authentication"
        ) from exc
    finally:
        _zeroize_bytearray(plaintext_dek)
        _zeroize_bytearray(plaintext)


def _require_plaintext(value: str) -> str:
    if not isinstance(value, str):
        raise EnvelopeEncryptionError(
            f"plaintext must be a string, got {type(value).__name__}"
        )
    if not value:
        raise EnvelopeEncryptionError("plaintext must not be empty")
    return value


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise EnvelopeEncryptionError(
            f"{name} must be a non-empty string, got {type(value).__name__}"
        )
    return value


def _context_dict(context: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in context.items():
        if not key or value is None:
            raise EnvelopeEncryptionError("encryption_context keys and values are required")
        out[str(key)] = str(value)
    return out


def _new_dek_id() -> str:
    return "dek_" + secrets.token_urlsafe(18)


def _dek_context(tenant_id: str, dek_id: str, purpose: str) -> dict[str, str]:
    return {
        "tenant_id": tenant_id,
        "dek_id": dek_id,
        "purpose": _require_str(purpose, "purpose"),
        "schema": "ks.1.2",
    }


def _aad(tenant_id: str, dek_id: str) -> bytes:
    return json.dumps(
        {
            "alg": AES_GCM_ALGORITHM,
            "dek": dek_id,
            "fmt": ENVELOPE_FORMAT_VERSION,
            "tid": tenant_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _zeroize_bytearray(buffer: bytearray) -> None:
    """Best-effort in-place wipe for mutable secret buffers."""

    if not buffer:
        return
    _sodium_memzero(buffer)


def _sodium_memzero(buffer: bytearray) -> None:
    view = sodium_core.ffi.from_buffer(buffer)
    try:
        sodium_core.lib.sodium_memzero(view, len(buffer))
    finally:
        del view


def _load_ciphertext_envelope(ciphertext: str) -> dict[str, Any]:
    try:
        envelope = json.loads(_require_str(ciphertext, "ciphertext"))
    except (TypeError, ValueError) as exc:
        raise CiphertextCorruptedError("ciphertext envelope is not valid JSON") from exc
    if not isinstance(envelope, dict):
        raise CiphertextCorruptedError(
            f"ciphertext envelope must be an object, got {type(envelope).__name__}"
        )
    if envelope.get("fmt") != ENVELOPE_FORMAT_VERSION:
        raise UnknownEnvelopeVersionError(
            f"unknown envelope fmt={envelope.get('fmt')!r}"
        )
    if envelope.get("alg") != AES_GCM_ALGORITHM:
        raise CiphertextCorruptedError(
            f"unsupported envelope alg={envelope.get('alg')!r}"
        )
    for key in ("dek", "tid", "nonce_b64", "ciphertext_b64"):
        _require_str(envelope.get(key), key)
    return envelope


def _check_binding(envelope: Mapping[str, Any], dek_ref: TenantDEKRef) -> None:
    if not hmac.compare_digest(str(envelope["dek"]), dek_ref.dek_id):
        raise BindingMismatchError("ciphertext bound to a different dek_id")
    if not hmac.compare_digest(str(envelope["tid"]), dek_ref.tenant_id):
        raise BindingMismatchError("ciphertext bound to a different tenant_id")


def _adapter_for_ref(
    dek_ref: TenantDEKRef,
    adapter: Optional[kms_adapters.KMSAdapter],
) -> kms_adapters.KMSAdapter:
    if adapter is not None:
        if adapter.provider != dek_ref.provider:
            raise BindingMismatchError(
                f"KMS adapter provider {adapter.provider!r} does not match {dek_ref.provider!r}"
            )
        return adapter
    if dek_ref.provider == kms_adapters.LocalFernetKMSAdapter.provider:
        return kms_adapters.LocalFernetKMSAdapter(key_id=dek_ref.key_id)
    raise EnvelopeEncryptionError(
        "kms_adapter is required to decrypt non-local TenantDEKRef"
    )


__all__ = [
    "AES_GCM_ALGORITHM",
    "BindingMismatchError",
    "CiphertextCorruptedError",
    "DEFAULT_PURPOSE",
    "DEK_RAW_BYTES",
    "ENVELOPE_ENABLED_ENV",
    "ENVELOPE_FORMAT_VERSION",
    "EnvelopeEncryptionError",
    "KEY_VERSION_CURRENT",
    "NONCE_RAW_BYTES",
    "TenantDEKRef",
    "UnknownEnvelopeVersionError",
    "decrypt",
    "encrypt",
    "is_enabled",
]

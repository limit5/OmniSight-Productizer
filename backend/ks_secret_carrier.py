"""KS envelope carrier helpers for legacy credential columns.

This module is intentionally small: it packs the existing KS.1.2
``ciphertext`` + ``TenantDEKRef`` pair into the JSON cell shape already
used by ``tenant_secrets`` / ``oauth_tokens`` and keeps a Fernet fallback
for pre-backfill rows.
"""
from __future__ import annotations

import hmac
import json
from typing import Mapping

from backend import secret_store
from backend.security import envelope as tenant_envelope


CARRIER_FORMAT_VERSION = 1
BINDING_FORMAT_VERSION = 1


def pack_secret(
    plaintext: str,
    tenant_id: str,
    *,
    purpose: str,
    binding: Mapping[str, str] | None = None,
) -> str:
    if not plaintext:
        return ""
    payload = json.dumps(
        {
            "fmt": BINDING_FORMAT_VERSION,
            "ctx": dict(binding or {}),
            "tok": plaintext,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    ciphertext, dek_ref = tenant_envelope.encrypt(
        payload,
        tenant_id,
        purpose=purpose,
    )
    return json.dumps(
        {
            "fmt": CARRIER_FORMAT_VERSION,
            "ciphertext": ciphertext,
            "dek_ref": dek_ref.to_dict(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def unpack_secret(
    stored: str,
    *,
    binding: Mapping[str, str] | None = None,
    legacy_fernet: bool = True,
) -> str:
    if not stored:
        return ""
    try:
        carrier = json.loads(stored)
    except (TypeError, ValueError):
        if legacy_fernet:
            return secret_store.decrypt(stored)
        raise
    if not isinstance(carrier, dict):
        raise ValueError("KS carrier must be an object")
    if carrier.get("fmt") != CARRIER_FORMAT_VERSION:
        raise ValueError("unknown KS carrier format")
    ciphertext = carrier.get("ciphertext")
    dek_ref_raw = carrier.get("dek_ref")
    if not isinstance(ciphertext, str) or not ciphertext:
        raise ValueError("KS carrier missing ciphertext")
    if not isinstance(dek_ref_raw, dict):
        raise ValueError("KS carrier missing dek_ref")

    dek_ref = tenant_envelope.TenantDEKRef.from_dict(dek_ref_raw)
    payload_raw = tenant_envelope.decrypt(ciphertext, dek_ref)
    payload = json.loads(payload_raw)
    if not isinstance(payload, dict):
        raise ValueError("KS binding must be an object")
    if payload.get("fmt") != BINDING_FORMAT_VERSION:
        raise ValueError("unknown KS binding format")
    expected = dict(binding or {})
    actual = payload.get("ctx")
    if not isinstance(actual, dict):
        raise ValueError("KS binding missing context")
    for key, value in expected.items():
        stored_value = actual.get(key)
        if not isinstance(stored_value, str) or not hmac.compare_digest(
            stored_value,
            value,
        ):
            raise ValueError(f"KS binding mismatch: {key}")
    token = payload.get("tok")
    if not isinstance(token, str):
        raise ValueError("KS binding missing plaintext")
    return token


def looks_like_carrier(stored: str) -> bool:
    try:
        carrier = json.loads(stored)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(carrier, dict)
        and carrier.get("fmt") == CARRIER_FORMAT_VERSION
        and isinstance(carrier.get("ciphertext"), str)
        and isinstance(carrier.get("dek_ref"), dict)
    )

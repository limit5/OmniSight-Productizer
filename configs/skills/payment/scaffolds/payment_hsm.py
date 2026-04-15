"""Payment HSM integration scaffold.

Adapt to your HSM vendor (Thales payShield / Utimaco / SafeNet Luna).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class HSMVendor(str, Enum):
    THALES = "thales"
    UTIMACO = "utimaco"
    SAFENET = "safenet"


@dataclass
class HSMConfig:
    vendor: HSMVendor
    host: str
    port: int
    timeout_ms: int = 5000
    tls_enabled: bool = True
    client_cert_path: str = ""
    client_key_path: str = ""


class HSMClient:
    """Abstract HSM client — subclass per vendor."""

    def __init__(self, config: HSMConfig):
        self.config = config
        self._connected = False

    def connect(self) -> None:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def generate_key(self, key_type: str, algorithm: str) -> dict[str, Any]:
        raise NotImplementedError

    def encrypt(self, key_id: str, plaintext: bytes) -> bytes:
        raise NotImplementedError

    def decrypt(self, key_id: str, ciphertext: bytes) -> bytes:
        raise NotImplementedError

    def sign(self, key_id: str, data: bytes) -> bytes:
        raise NotImplementedError

    def verify(self, key_id: str, data: bytes, signature: bytes) -> bool:
        raise NotImplementedError

    def inject_key(self, device_id: str, key_type: str) -> dict[str, Any]:
        raise NotImplementedError


class ThalesHSMClient(HSMClient):
    """Thales payShield 10K client scaffold."""

    def connect(self) -> None:
        # TODO: Implement Thales host command protocol connection
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def generate_key(self, key_type: str, algorithm: str) -> dict[str, Any]:
        # Thales command A0 — Generate Key
        return {"command": "A0", "key_type": key_type, "algorithm": algorithm}

    def encrypt(self, key_id: str, plaintext: bytes) -> bytes:
        # Thales command M0 — Encrypt Data
        return b"(thales-encrypted)"

    def decrypt(self, key_id: str, ciphertext: bytes) -> bytes:
        # Thales command — Decrypt Data
        return b"(thales-decrypted)"


class UtimacoHSMClient(HSMClient):
    """Utimaco CryptoServer client scaffold."""

    def connect(self) -> None:
        # TODO: Implement PKCS#11 or REST API connection
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def generate_key(self, key_type: str, algorithm: str) -> dict[str, Any]:
        return {"command": "CXI_KEY_GENERATE", "key_type": key_type, "algorithm": algorithm}

    def encrypt(self, key_id: str, plaintext: bytes) -> bytes:
        return b"(utimaco-encrypted)"

    def decrypt(self, key_id: str, ciphertext: bytes) -> bytes:
        return b"(utimaco-decrypted)"


class SafeNetHSMClient(HSMClient):
    """Thales Luna (SafeNet) client scaffold."""

    def connect(self) -> None:
        # TODO: Implement PKCS#11 session
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def generate_key(self, key_type: str, algorithm: str) -> dict[str, Any]:
        return {"command": "C_GenerateKey", "key_type": key_type, "algorithm": algorithm}

    def encrypt(self, key_id: str, plaintext: bytes) -> bytes:
        return b"(safenet-encrypted)"

    def decrypt(self, key_id: str, ciphertext: bytes) -> bytes:
        return b"(safenet-decrypted)"


def create_hsm_client(config: HSMConfig) -> HSMClient:
    clients = {
        HSMVendor.THALES: ThalesHSMClient,
        HSMVendor.UTIMACO: UtimacoHSMClient,
        HSMVendor.SAFENET: SafeNetHSMClient,
    }
    cls = clients.get(config.vendor)
    if cls is None:
        raise ValueError(f"Unsupported HSM vendor: {config.vendor}")
    return cls(config)

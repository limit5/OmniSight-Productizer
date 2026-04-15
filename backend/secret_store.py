"""B12 — Secrets store for at-rest encryption of sensitive tokens.

Provides Fernet-based symmetric encryption for storing API tokens
(Cloudflare, etc.) so they never appear in plaintext on disk or in
the database. The UI only sees a fingerprint (last 4 chars).

The encryption key is derived from OMNISIGHT_SECRET_KEY env var.
If unset, a machine-local key is auto-generated in data/.secret_key.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_KEY_PATH = _PROJECT_ROOT / "data" / ".secret_key"

_fernet = None


def _get_key() -> bytes:
    env_key = os.environ.get("OMNISIGHT_SECRET_KEY", "").strip()
    if env_key:
        return base64.urlsafe_b64encode(hashlib.sha256(env_key.encode()).digest())
    if _KEY_PATH.exists():
        return _KEY_PATH.read_bytes().strip()
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    _KEY_PATH.write_bytes(key)
    _KEY_PATH.chmod(0o600)
    logger.info("Generated new secret key at %s", _KEY_PATH)
    return key


def _get_fernet():
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet
        _fernet = Fernet(_get_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")


def fingerprint(token: str) -> str:
    if len(token) <= 8:
        return "****"
    return f"…{token[-4:]}"


def _reset_for_tests():
    global _fernet
    _fernet = None

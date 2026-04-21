"""B12 — Secrets store for at-rest encryption of sensitive tokens.

Provides Fernet-based symmetric encryption for storing API tokens
(Cloudflare, etc.) so they never appear in plaintext on disk or in
the database. The UI only sees a fingerprint (last 4 chars).

The encryption key is derived from OMNISIGHT_SECRET_KEY env var.
If unset, a machine-local key is auto-generated in data/.secret_key.

Task #104 / Step B.3 (2026-04-21): first-boot key generation now
takes an ``fcntl.flock`` on the key path's parent directory so
concurrent ``uvicorn --workers N`` startups don't each generate
their own Fernet key, race the disk write, and end up with
divergent in-memory keys that can't decrypt each other's ciphertext.

The fix is a classic double-checked-locking pattern with OS-level
file locking:
  1. Ensure parent dir exists.
  2. Open ``<parent>/.secret_key.lock`` for R/W, flock LOCK_EX.
  3. Re-check the real key file under the lock. If present, read.
     If absent, generate + write.
  4. Release lock.

POSIX-only (fcntl). The codebase runs on Linux in prod per
CLAUDE.md; Windows would need ``msvcrt.locking`` which is out of
scope here.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_KEY_PATH = _PROJECT_ROOT / "data" / ".secret_key"
_KEY_LOCK_PATH = _PROJECT_ROOT / "data" / ".secret_key.lock"

_fernet = None


def _get_key() -> bytes:
    env_key = os.environ.get("OMNISIGHT_SECRET_KEY", "").strip()
    if env_key:
        return base64.urlsafe_b64encode(hashlib.sha256(env_key.encode()).digest())

    # Fast-path: file already exists, no lock needed for a plain
    # read. Readers don't need coordination among themselves —
    # only the first-generate race matters.
    if _KEY_PATH.exists():
        data = _KEY_PATH.read_bytes().strip()
        if data:
            return data
        # Empty file (partial write from a prior-crashed worker) —
        # fall through to the locked re-generate path.

    # Slow path: file missing / empty. Acquire an exclusive flock
    # on the lock file, then double-check + generate if still needed.
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(
        str(_KEY_LOCK_PATH),
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # Re-check under the lock. Another worker may have generated
        # the key while we were waiting to acquire the flock.
        if _KEY_PATH.exists():
            data = _KEY_PATH.read_bytes().strip()
            if data:
                logger.debug(
                    "Secret key already written by another worker; "
                    "using existing key."
                )
                return data
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        # Write atomically: write to tmp + rename. Prevents a
        # crashed worker from leaving a partial-write behind that
        # the fast-path above would then read.
        tmp = _KEY_PATH.with_suffix(_KEY_PATH.suffix + ".tmp")
        tmp.write_bytes(key)
        tmp.chmod(0o600)
        os.replace(str(tmp), str(_KEY_PATH))
        logger.info("Generated new secret key at %s", _KEY_PATH)
        return key
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


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

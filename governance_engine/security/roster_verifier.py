"""Roster GPG signature verifier (ADR-0033 §3, OP-1095).

Verifies the L1 signature on the Deputy roster YAML before any L2
deputy authority is honored. Shells out to ``gpg --verify`` — no
extra Python dep, audit-friendly (operator can replay the command).
"""
from __future__ import annotations


import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "VerifyResult", "DEFAULT_FINGERPRINT_PATH", "DEFAULT_ROSTER_PATH",
    "load_l1_fingerprint", "verify_roster_signature",
]

logger = logging.getLogger(__name__)

DEFAULT_FINGERPRINT_PATH = Path("~/.config/omnisight/governance-l1-fingerprint").expanduser()
DEFAULT_ROSTER_PATH = Path("~/.config/omnisight/operator-deputy-roster.yaml").expanduser()

_VERIFY_TIMEOUT_SEC = 10


@dataclass(frozen=True)
class VerifyResult:
    is_valid: bool
    signer_fingerprint: str | None = None
    error: str | None = None


def _normalize_fingerprint(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip().replace(" ", "").upper()
    return stripped or None


def load_l1_fingerprint(path: Path | None = None) -> str | None:
    """Read the trusted L1 fingerprint from its canonical file."""
    fp_path = path if path is not None else DEFAULT_FINGERPRINT_PATH
    if not fp_path.is_file():
        return None
    return _normalize_fingerprint(fp_path.read_text(encoding="utf-8"))


def _parse_signer_fingerprint(status_output: str) -> str | None:
    # gpg --status-fd=1 emits "[GNUPG:] VALIDSIG <fpr> <date> ...".
    for line in status_output.splitlines():
        if not line.startswith("[GNUPG:] VALIDSIG"):
            continue
        parts = line.split()
        if len(parts) >= 3:
            return parts[2].upper()
    return None


def verify_roster_signature(
    roster_path: Path,
    l1_fingerprint: str,
    *,
    gpg_binary: str = "gpg",
) -> VerifyResult:
    """Verify ``roster_path`` against its ``.asc`` companion signature."""
    expected = _normalize_fingerprint(l1_fingerprint)
    if expected is None:
        return VerifyResult(is_valid=False, error="trusted L1 fingerprint is empty")

    if not roster_path.is_file():
        return VerifyResult(is_valid=False, error=f"roster file not found: {roster_path}")

    sig_path = roster_path.with_suffix(roster_path.suffix + ".asc")
    if not sig_path.is_file():
        return VerifyResult(is_valid=False, error=f"signature file not found: {sig_path}")

    cmd = [
        gpg_binary, "--batch", "--no-tty", "--status-fd=1",
        "--verify", str(sig_path), str(roster_path),
    ]
    try:
        completed = subprocess.run(
            cmd, check=False, capture_output=True, text=True,
            timeout=_VERIFY_TIMEOUT_SEC,
        )
    except FileNotFoundError:
        logger.warning("gpg binary not found: %s", gpg_binary)
        return VerifyResult(is_valid=False, error="gpg not installed")
    except subprocess.TimeoutExpired:
        return VerifyResult(is_valid=False, error="gpg verify timed out")
    except OSError as exc:
        return VerifyResult(is_valid=False, error=f"gpg invocation failed: {exc}")

    signer = _parse_signer_fingerprint(completed.stdout)
    if completed.returncode != 0:
        return VerifyResult(
            is_valid=False, signer_fingerprint=signer,
            error=f"gpg verify failed (rc={completed.returncode})",
        )
    if signer is None:
        return VerifyResult(
            is_valid=False,
            error="gpg verify exited 0 but emitted no VALIDSIG status line",
        )
    if signer != expected:
        return VerifyResult(
            is_valid=False, signer_fingerprint=signer,
            error="signer fingerprint does not match trusted L1 fingerprint",
        )
    return VerifyResult(is_valid=True, signer_fingerprint=signer)

"""AS.2.1 — OAuth token vault: per-user / per-provider at-rest encryption.

Encrypts OAuth ``access_token`` / ``refresh_token`` material for storage
in the ``oauth_tokens`` row that AS.2.2 will land. The vault is the
*only* approved entry-point for OAuth-token ciphertext; every router
that touches a stored OAuth credential must round-trip through
:func:`encrypt_for_user` / :func:`decrypt_for_user` so the
ciphertext-shuffling guard, the master-Fernet-key invariant, and the
``key_version`` forward-promotion hook stay honoured uniformly.

Cryptographic shape
───────────────────
The vault wraps each plaintext in a small JSON envelope before handing
it to ``backend.secret_store.encrypt`` (the project-wide Fernet cipher).
The envelope is::

    {
      "fmt": 1,                # binding-format version
      "salt": "<b64 16 bytes>", # per-row random — see below
      "uid":  "<owner user_id>",
      "prv":  "<provider slug>",
      "tok":  "<plaintext token>"
    }

On decrypt the envelope is parsed inside the Fernet authenticated
boundary; ``uid`` and ``prv`` MUST match the values the caller claims
the row belongs to, otherwise :class:`BindingMismatchError` is raised.
The salt is purely the "per-row" half of the binding — it gives every
ciphertext a fresh nonce-like tag inside the auth-checked envelope so a
DB-level row swap (attacker copies user-A's ``access_token_enc`` into
user-B's row) decrypts but fails the binding check at the application
layer.

Why a binding envelope and not a derived sub-key
────────────────────────────────────────────────
AS.0.4 §3.1 hard invariant: the OAuth token vault MUST reuse
``backend.secret_store._fernet`` and MUST NOT derive a per-row sub-key
or introduce a second master key. The binding envelope satisfies the
TODO row's "per-user salt" intent — defence against ciphertext
shuffling — while staying inside the single-master-key contract:

  * Single Fernet key (audit-friendly, one key-rotation runbook).
  * Per-row salt is *bound* to user_id+provider via the envelope, not
    *used* to derive a separate Fernet key.
  * Same primitives every other secret_store caller uses
    (``git_credentials`` / ``llm_credentials`` / ``codesign_store``).

``key_version`` reservation (AS.0.4 §3.1 / §3.2)
────────────────────────────────────────────────
``oauth_tokens.key_version INTEGER DEFAULT 1`` is reserved as a future
KMS-rotation hook. In this release every ciphertext is written and
read at :data:`KEY_VERSION_CURRENT` = 1; any other value on read
raises :class:`UnknownKeyVersionError`. The roadmap (AS.0.4 §3.1 #4)
lays out the multi-version dual-read flow that will land alongside
the first KMS migration; until then the vault refuses unknown
versions instead of silently degrading.

Provider whitelist
──────────────────
:data:`SUPPORTED_PROVIDERS` MUST equal
``account_linking._AS1_OAUTH_PROVIDERS`` byte-for-byte (AS.0.4 §5.2
drift guard — enforced by ``test_token_vault.py``). Adding a new
provider requires touching both modules in the same PR.

Path deviation note (per AS.1.1 / AS.1.3 / AS.1.4 precedent)
────────────────────────────────────────────────────────────
The AS.0.4 / TODO canonical path is ``backend/auth/token_vault.py``,
but the legacy ``backend/auth.py`` session/RBAC module (~140 import
sites) occupies that namespace; promoting it to a package is an
independent refactor outside this row's scope. The vault lives in
``backend/security/`` parallel to the AS.1.x sibling modules
(``oauth_client.py`` / ``oauth_vendors.py`` / ``oauth_audit.py``)
and the AS.0.10 password generator. Future ``backend/auth.py``-
to-package consolidation rolls these in together.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* No module-level mutable state — only frozen dataclasses, immutable
  tuples / strings, and ``frozenset``s.
* All randomness comes from :mod:`secrets` (kernel CSPRNG).
* The Fernet key fetch is delegated to :mod:`backend.secret_store`,
  which already owns the ``fcntl.flock`` first-boot generation guard
  (task #104 — answer #2 of SOP §1, coordinated via on-disk lock).
* No DB writes, no network IO. Persistence is the caller's job.
* Importing the module is free of side effects.

TS twin (AS.2.3 — forward-reserved)
───────────────────────────────────
``templates/_shared/token-vault/`` will land in AS.2.3 — a libsodium
(secretbox) equivalent for generated app workspaces (Web Crypto +
NaCl-style API). The wrapper / binding shape MUST stay byte-equal so
a server-encrypted ciphertext is not a hard requirement for the twin
to read; the twin operates on its own tokens with the same envelope
fields (``fmt`` / ``salt`` / ``uid`` / ``prv`` / ``tok``).

AS.0.8 single-knob hook
───────────────────────
:func:`is_enabled` reads ``settings.as_enabled`` via ``getattr``
fallback (forward-promotion guard, mirrors AS.1.1 / AS.0.9 pattern).
The pure helpers (:func:`encrypt_for_user`, :func:`decrypt_for_user`)
deliberately do **NOT** auto-gate on the knob: per AS.0.4 §6.2 a
backfill / DSAR / key-rotation script must remain able to read
existing ciphertext even when OAuth login is feature-flagged off
(``OMNISIGHT_AS_ENABLED=false``). Caller-level endpoints (AS.6.x)
short-circuit before reaching the vault when the knob is off.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Optional

from backend import secret_store

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

#: Active key version. AS.0.4 §3.1 reserves the column for future
#: KMS migrations; the first KMS-rotation row will introduce a v2
#: branch and the dual-read fallback. Any non-1 value on decrypt
#: raises :class:`UnknownKeyVersionError`.
KEY_VERSION_CURRENT: int = 1

#: Binding envelope format. Bumped only when the wrapper shape itself
#: changes (e.g. add a field). Changing this requires a dual-read
#: phase; do NOT alter casually.
BINDING_FORMAT_VERSION: int = 1

#: Per-row salt size (bytes). 16 bytes / 128 bits is comfortably above
#: the 64-bit collision floor for any realistic OmniSight-scale row
#: count and matches the GUID / nonce convention elsewhere in the
#: codebase. The salt is part of the binding envelope, not a key
#: derivation input — see module docstring.
_SALT_RAW_BYTES = 16

#: Provider whitelist — MUST byte-equal ``_AS1_OAUTH_PROVIDERS`` in
#: :mod:`backend.account_linking`. The drift guard test in
#: :mod:`backend.tests.test_token_vault` fails red when the two
#: diverge; AS.0.4 §5.2 codifies this as a hard cross-module invariant.
SUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {"google", "github", "apple", "microsoft"}
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TokenVaultError(Exception):
    """Base class for vault-layer errors. Lets callers
    ``except TokenVaultError`` once instead of enumerating each
    subclass."""


class UnsupportedProviderError(TokenVaultError, ValueError):
    """``provider`` is not in :data:`SUPPORTED_PROVIDERS`. Subclasses
    ``ValueError`` so call sites that already catch ``ValueError`` for
    input validation continue to work."""


class UnknownKeyVersionError(TokenVaultError):
    """``key_version`` on a stored row is not :data:`KEY_VERSION_CURRENT`.
    The first KMS migration will replace this with a multi-version
    dispatch; until then any unknown value is treated as corruption."""


class BindingMismatchError(TokenVaultError):
    """The decrypted binding envelope's ``uid`` / ``prv`` did not match
    the values the caller claimed the row belongs to. Indicates either
    a DB-level row shuffle (rare; would still need master-key access to
    forge new ciphertext) or a caller bug passing the wrong user_id /
    provider on decrypt."""


class CiphertextCorruptedError(TokenVaultError):
    """The ciphertext could not be decrypted (Fernet auth failed) or
    the inner JSON envelope was malformed. Re-raised as a vault-layer
    error so callers don't need to special-case the underlying Fernet
    InvalidToken exception."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Frozen dataclasses (public surface)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class EncryptedToken:
    """Persistent shape for one stored OAuth credential.

    Round-tripped to / from the ``oauth_tokens`` row's
    ``access_token_enc`` (TEXT) + ``key_version`` (INTEGER) columns.
    The ``ciphertext`` is the Fernet token (urlsafe-base64 ASCII), and
    ``key_version`` MUST be :data:`KEY_VERSION_CURRENT` for this
    release. The salt is intentionally NOT a public attribute — it
    lives inside the Fernet-authenticated envelope, not in a separate
    column.
    """

    ciphertext: str
    key_version: int


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AS.0.8 single-knob hook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def is_enabled() -> bool:
    """Whether the AS feature family is enabled per AS.0.8 §3.1.

    Reads ``settings.as_enabled`` via ``getattr`` fallback (default
    ``True``) so this module loads cleanly before AS.3.1 lands the
    field on :class:`backend.config.Settings` — same forward-promotion
    pattern AS.1.1 / AS.0.9 use.

    The vault's pure helpers do NOT auto-call this hook (per module
    docstring, backfill / DSAR / key-rotation paths must keep working
    knob-off). Caller endpoints (AS.6.x) gate on it before reaching
    the vault.
    """
    try:
        from backend.config import settings
    except Exception:  # pragma: no cover — config import never fails in prod
        return True
    return bool(getattr(settings, "as_enabled", True))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internal helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _check_provider(provider: str) -> str:
    if not isinstance(provider, str) or not provider:
        raise UnsupportedProviderError(
            f"provider must be a non-empty string, got {type(provider).__name__}"
        )
    p = provider.strip().lower()
    if p not in SUPPORTED_PROVIDERS:
        raise UnsupportedProviderError(
            f"unsupported OAuth provider: {provider!r} "
            f"(expected one of {sorted(SUPPORTED_PROVIDERS)})"
        )
    return p


def _check_user_id(user_id: str) -> str:
    if not isinstance(user_id, str) or not user_id:
        raise TokenVaultError(
            f"user_id must be a non-empty string, got {type(user_id).__name__}"
        )
    return user_id


def _check_plaintext(plaintext: str) -> str:
    if not isinstance(plaintext, str):
        raise TokenVaultError(
            f"plaintext must be a string, got {type(plaintext).__name__}"
        )
    if not plaintext:
        # Empty string would survive Fernet round-trip but defeats the
        # purpose of the vault (nothing to protect, and ``fingerprint``
        # would surface ``****`` placeholder). Reject so callers don't
        # accidentally write empty rows.
        raise TokenVaultError("plaintext must not be empty")
    return plaintext


def _b64_salt() -> tuple[bytes, str]:
    raw = secrets.token_bytes(_SALT_RAW_BYTES)
    return raw, base64.b64encode(raw).decode("ascii")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def encrypt_for_user(
    user_id: str,
    provider: str,
    plaintext: str,
) -> EncryptedToken:
    """Encrypt *plaintext* (an OAuth access_token / refresh_token) for
    storage in the ``oauth_tokens`` row owned by *user_id* + *provider*.

    The plaintext is wrapped in a binding envelope (see module
    docstring) before being handed to :func:`backend.secret_store.encrypt`,
    so the resulting ciphertext is bound to this *(user_id, provider)*
    pair: a row swap in the database will be caught by
    :func:`decrypt_for_user`.

    Raises
    ------
    UnsupportedProviderError
        *provider* is not in :data:`SUPPORTED_PROVIDERS`.
    TokenVaultError
        *user_id* or *plaintext* is missing / not a string.
    """

    p = _check_provider(provider)
    uid = _check_user_id(user_id)
    tok = _check_plaintext(plaintext)
    _, salt_b64 = _b64_salt()

    envelope = {
        "fmt": BINDING_FORMAT_VERSION,
        "salt": salt_b64,
        "uid": uid,
        "prv": p,
        "tok": tok,
    }
    # ``sort_keys`` + ``separators`` give a deterministic byte layout
    # inside the ciphertext — useful for any future audit / forensic
    # path that wants to assert envelope shape without re-running the
    # encryption.
    payload = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    ciphertext = secret_store.encrypt(payload)
    return EncryptedToken(
        ciphertext=ciphertext,
        key_version=KEY_VERSION_CURRENT,
    )


def decrypt_for_user(
    user_id: str,
    provider: str,
    token: EncryptedToken,
) -> str:
    """Decrypt *token* and return its plaintext.

    Verifies that the binding envelope inside the ciphertext matches
    the *(user_id, provider)* the caller is claiming the row belongs
    to. A mismatch (DB-level row shuffle, or caller bug) raises
    :class:`BindingMismatchError`.

    Raises
    ------
    UnsupportedProviderError
        *provider* is not in :data:`SUPPORTED_PROVIDERS`.
    UnknownKeyVersionError
        ``token.key_version`` is not :data:`KEY_VERSION_CURRENT`.
    BindingMismatchError
        The ciphertext was encrypted for a different *(user_id,
        provider)* pair, or for a different binding-format version.
    CiphertextCorruptedError
        Fernet authentication failed, or the inner JSON envelope is
        malformed.
    """

    p = _check_provider(provider)
    uid = _check_user_id(user_id)
    if not isinstance(token, EncryptedToken):
        raise TokenVaultError(
            f"token must be an EncryptedToken, got {type(token).__name__}"
        )
    if token.key_version != KEY_VERSION_CURRENT:
        raise UnknownKeyVersionError(
            f"unknown key_version={token.key_version!r} "
            f"(this release supports only {KEY_VERSION_CURRENT})"
        )

    try:
        payload = secret_store.decrypt(token.ciphertext)
    except Exception as exc:  # cryptography.fernet.InvalidToken et al.
        raise CiphertextCorruptedError(
            "ciphertext failed Fernet authentication"
        ) from exc

    try:
        envelope = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise CiphertextCorruptedError("inner envelope is not valid JSON") from exc
    if not isinstance(envelope, dict):
        raise CiphertextCorruptedError(
            f"inner envelope must be an object, got {type(envelope).__name__}"
        )

    fmt = envelope.get("fmt")
    if fmt != BINDING_FORMAT_VERSION:
        # Treat as binding mismatch rather than corruption — the
        # ciphertext decoded fine (Fernet auth passed), it just
        # carries a shape this release doesn't understand.
        raise BindingMismatchError(
            f"unknown binding format version: {fmt!r} "
            f"(this release supports only {BINDING_FORMAT_VERSION})"
        )

    stored_uid = envelope.get("uid")
    stored_prv = envelope.get("prv")
    if not isinstance(stored_uid, str) or not isinstance(stored_prv, str):
        raise CiphertextCorruptedError(
            "envelope missing 'uid' / 'prv' string fields"
        )

    # Constant-time compare — both sides are short ASCII strings; a
    # timing leak here is theoretical at best (the attacker would need
    # to already control encrypted-row-shuffling AND timing-side-channel
    # a server response), but the stdlib gives it for free so we use it.
    if not hmac.compare_digest(stored_uid, uid):
        raise BindingMismatchError(
            "ciphertext bound to a different user_id"
        )
    if not hmac.compare_digest(stored_prv, p):
        raise BindingMismatchError(
            "ciphertext bound to a different provider"
        )

    plaintext = envelope.get("tok")
    if not isinstance(plaintext, str):
        raise CiphertextCorruptedError(
            "envelope 'tok' field missing or not a string"
        )
    return plaintext


def fingerprint(token: str) -> str:
    """Last-4-character fingerprint for UI / log surfacing.

    Thin re-export of :func:`backend.secret_store.fingerprint` so
    vault callers don't need to import a second module just to redact
    a token in a structured log line.
    """
    return secret_store.fingerprint(token or "")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


__all__ = [
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
]

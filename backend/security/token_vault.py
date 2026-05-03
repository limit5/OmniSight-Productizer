"""AS.2.1 / KS.1.3 — OAuth token vault: per-user / per-provider encryption.

Encrypts OAuth ``access_token`` / ``refresh_token`` material for storage
in the ``oauth_tokens`` row that AS.2.2 will land. The vault is the
*only* approved entry-point for OAuth-token ciphertext; every router
that touches a stored OAuth credential must round-trip through
:func:`encrypt_for_user` / :func:`decrypt_for_user` so the
ciphertext-shuffling guard and the ``key_version`` promotion hook stay
honoured uniformly.

Cryptographic shape
───────────────────
The vault wraps each plaintext in a small JSON binding envelope before
handing it to ``backend.security.envelope.encrypt`` (KS.1.2 per-tenant
DEK envelope encryption). The binding payload is::

    {
      "fmt": 1,                # binding-format version
      "salt": "<b64 16 bytes>", # per-row random — see below
      "uid":  "<owner user_id>",
      "prv":  "<provider slug>",
      "tok":  "<plaintext token>"
    }

On decrypt the binding payload is parsed inside the KS.1 envelope
authenticated boundary; ``uid`` and ``prv`` MUST match the values the
caller claims the row belongs to, otherwise
:class:`BindingMismatchError` is raised.
The salt is purely the "per-row" half of the binding — it gives every
ciphertext a fresh nonce-like tag inside the auth-checked envelope so a
DB-level row swap (attacker copies user-A's ``access_token_enc`` into
user-B's row) decrypts but fails the binding check at the application
layer.

KS.1.3 migration window
───────────────────────
New writes store a compact JSON token envelope in
``oauth_tokens.access_token_enc`` / ``refresh_token_enc``::

    {
      "fmt": 1,
      "ciphertext": "<KS.1.2 ciphertext JSON>",
      "dek_ref": { ... TenantDEKRef ... }
    }

Existing rows written by AS.2.1 carry the same ``key_version = 1`` but
plain Fernet ciphertext from :mod:`backend.secret_store`. During the
30-day KS.1.3 migration window, reads fall back to that legacy Fernet
path only when the stored ciphertext is not a token-envelope JSON
object. After ``LEGACY_FERNET_FALLBACK_DEPRECATES_ON`` the fallback
raises :class:`LegacyFernetFallbackDeprecatedError`; writers never
produce legacy Fernet ciphertext in this release.

``key_version`` / master-KEK rotation (AS.0.4 §3.1 / KS.1.4)
────────────────────────────────────────────────────────────
``oauth_tokens.key_version`` is the coarse-grained master-KEK epoch
for token-vault rows. New writes call :func:`current_key_version`,
which derives the active version from a fixed quarterly UTC schedule
(``KEY_VERSION_ROTATION_INTERVAL_DAYS = 90``). Reads accept any
version from :data:`KEY_VERSION_INITIAL` through the current scheduled
version, so old rows keep decrypting after a quarter flips. Callers
that want background lazy re-encrypt use
:func:`decrypt_for_user_with_lazy_reencrypt`: it returns plaintext and
an optional replacement :class:`EncryptedToken` when the stored
``key_version`` lags the schedule. The caller owns the SQL
``UPDATE ... WHERE version = old_version`` optimistic-lock write.

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
  dates / tuples / strings, and ``frozenset``s.
* All randomness comes from :mod:`secrets` (kernel CSPRNG).
* KS.1 envelope local fallback delegates KEK material to
  :mod:`backend.secret_store`, which already owns the ``fcntl.flock``
  first-boot generation guard (task #104 — answer #2 of SOP §1,
  coordinated via on-disk lock).
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
import datetime as _dt
import hmac
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from backend import secret_store
from backend.security import decryption_audit
from backend.security import envelope as tenant_envelope

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

#: First token-vault master-KEK epoch. Existing KS.1.3 rows and legacy
#: Fernet fallback rows both carry this value.
KEY_VERSION_INITIAL: int = 1

#: Active token-vault key version at this release's landing date.
#: Runtime writes call :func:`current_key_version` so the quarterly
#: schedule can advance without a deploy.
KEY_VERSION_CURRENT: int = 1

#: Legacy AS.2.1 rows used ``backend.secret_store`` Fernet ciphertext
#: with ``key_version = 1``. KS.1.3 keeps a 30-day read fallback only.
KEY_VERSION_LEGACY_FERNET: int = 1

#: KS.1.4 quarterly rotation cadence. Every worker derives the same
#: answer from UTC dates; no module-global mutable state or scheduler
#: singleton is used.
KEY_VERSION_ROTATION_INTERVAL_DAYS: int = 90

#: First day the KS.1.4 automatic rotation schedule is active. The
#: first interval (2026-05-03 through 2026-07-31 UTC) remains v1; the
#: schedule flips to v2 on 2026-08-01 UTC.
KEY_VERSION_ROTATION_STARTED_ON = _dt.date(2026, 5, 3)

#: Binding envelope format. Bumped only when the wrapper shape itself
#: changes (e.g. add a field). Changing this requires a dual-read
#: phase; do NOT alter casually.
BINDING_FORMAT_VERSION: int = 1

#: Outer JSON format for the token-vault carrier stored in
#: ``oauth_tokens.access_token_enc`` / ``refresh_token_enc``. The
#: inner KS.1.2 ciphertext has its own ``fmt``.
TOKEN_ENVELOPE_FORMAT_VERSION: int = 1

#: KS.1.3 legacy fallback window. The start date is this row's landing
#: date; the old Fernet reader deprecates 30 calendar days later.
LEGACY_FERNET_FALLBACK_STARTED_ON = _dt.date(2026, 5, 3)
LEGACY_FERNET_FALLBACK_DEPRECATES_ON = _dt.date(2026, 6, 2)

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
    """``key_version`` on a stored row is outside the supported
    quarterly master-KEK schedule."""


class LegacyFernetFallbackDeprecatedError(UnknownKeyVersionError):
    """A legacy Fernet row was read after the KS.1.3 fallback deadline."""


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


class UnknownTokenEnvelopeVersionError(TokenVaultError):
    """The KS.1.3 token-envelope carrier is not a supported format."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Frozen dataclasses (public surface)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class EncryptedToken:
    """Persistent shape for one stored OAuth credential.

    Round-tripped to / from the ``oauth_tokens`` row's
    ``access_token_enc`` (TEXT) + ``key_version`` (INTEGER) columns.
    For new KS.1.3 writes the ``ciphertext`` is the token envelope
    JSON. For legacy AS.2.1 rows with the same ``key_version=1``, it
    is the Fernet token accepted only during the migration fallback
    window. The salt is
    intentionally NOT a public attribute — it lives inside the
    authenticated binding payload, not in a separate column.
    """

    ciphertext: str
    key_version: int


@dataclass(frozen=True)
class DecryptedToken:
    """Plaintext plus an optional lazy-reencrypt replacement.

    ``replacement`` is ``None`` when the stored row is already at the
    current scheduled key version. When present, the caller persists it
    back to ``oauth_tokens`` with the row's existing optimistic-lock
    ``version`` guard; this helper deliberately performs no DB IO.
    """

    plaintext: str
    replacement: Optional[EncryptedToken]
    key_version: int
    target_key_version: int


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


def _token_envelope(ciphertext: str, dek_ref: tenant_envelope.TenantDEKRef) -> str:
    payload = {
        "fmt": TOKEN_ENVELOPE_FORMAT_VERSION,
        "ciphertext": ciphertext,
        "dek_ref": dek_ref.to_dict(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _load_token_envelope(ciphertext: str) -> tuple[str, tenant_envelope.TenantDEKRef]:
    try:
        payload = json.loads(ciphertext)
    except (TypeError, ValueError) as exc:
        raise CiphertextCorruptedError("token envelope is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CiphertextCorruptedError(
            f"token envelope must be an object, got {type(payload).__name__}"
        )
    if payload.get("fmt") != TOKEN_ENVELOPE_FORMAT_VERSION:
        raise UnknownTokenEnvelopeVersionError(
            f"unknown token envelope fmt={payload.get('fmt')!r}"
        )
    inner_ciphertext = payload.get("ciphertext")
    if not isinstance(inner_ciphertext, str) or not inner_ciphertext:
        raise CiphertextCorruptedError("token envelope missing ciphertext")
    dek_ref_raw = payload.get("dek_ref")
    if not isinstance(dek_ref_raw, dict):
        raise CiphertextCorruptedError("token envelope missing dek_ref object")
    try:
        dek_ref = tenant_envelope.TenantDEKRef.from_dict(dek_ref_raw)
    except tenant_envelope.UnknownEnvelopeVersionError as exc:
        raise UnknownTokenEnvelopeVersionError(str(exc)) from exc
    except tenant_envelope.EnvelopeEncryptionError as exc:
        raise CiphertextCorruptedError("token envelope dek_ref is malformed") from exc
    return inner_ciphertext, dek_ref


def _tenant_id_for_user(user_id: str, tenant_id: Optional[str]) -> str:
    return _check_user_id(tenant_id) if tenant_id is not None else user_id


def _request_id_or_new(request_id: Optional[str]) -> str:
    return _check_user_id(request_id) if request_id is not None else str(uuid.uuid4())


def _utc_today() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def current_key_version(*, as_of: Optional[_dt.date] = None) -> int:
    """Return the quarterly scheduled master-KEK version.

    No shared runtime state: every worker derives the same value from
    UTC date constants, so cross-worker consistency does not depend on
    an in-memory singleton.
    """

    day = as_of or _utc_today()
    if day < KEY_VERSION_ROTATION_STARTED_ON:
        return KEY_VERSION_INITIAL
    elapsed_days = (day - KEY_VERSION_ROTATION_STARTED_ON).days
    return KEY_VERSION_INITIAL + (
        elapsed_days // KEY_VERSION_ROTATION_INTERVAL_DAYS
    )


def key_version_needs_lazy_reencrypt(
    key_version: int,
    *,
    as_of: Optional[_dt.date] = None,
) -> bool:
    """Whether a stored row lags the active quarterly KEK epoch."""

    return _check_key_version(key_version, as_of=as_of) < current_key_version(
        as_of=as_of
    )


def legacy_fernet_fallback_is_active(*, as_of: Optional[_dt.date] = None) -> bool:
    """Whether KS.1.3 should still read legacy Fernet token rows.

    No shared runtime state: every worker derives the same answer from
    UTC date constants baked into the row.
    """

    day = as_of or _utc_today()
    return day < LEGACY_FERNET_FALLBACK_DEPRECATES_ON


def _check_key_version(
    key_version: int,
    *,
    as_of: Optional[_dt.date] = None,
) -> int:
    if not isinstance(key_version, int):
        raise UnknownKeyVersionError(
            f"key_version must be an integer, got {type(key_version).__name__}"
        )
    current = current_key_version(as_of=as_of)
    if key_version < KEY_VERSION_INITIAL or key_version > current:
        raise UnknownKeyVersionError(
            f"unknown key_version={key_version!r} "
            f"(this release supports {KEY_VERSION_INITIAL}..{current})"
        )
    return key_version


def _binding_payload(user_id: str, provider: str, plaintext: str) -> str:
    _, salt_b64 = _b64_salt()
    payload = {
        "fmt": BINDING_FORMAT_VERSION,
        "salt": salt_b64,
        "uid": user_id,
        "prv": provider,
        "tok": plaintext,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _load_binding_payload(payload: str, user_id: str, provider: str) -> str:
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

    if not hmac.compare_digest(stored_uid, user_id):
        raise BindingMismatchError(
            "ciphertext bound to a different user_id"
        )
    if not hmac.compare_digest(stored_prv, provider):
        raise BindingMismatchError(
            "ciphertext bound to a different provider"
        )

    plaintext = envelope.get("tok")
    if not isinstance(plaintext, str):
        raise CiphertextCorruptedError(
            "envelope 'tok' field missing or not a string"
        )
    return plaintext


def _decrypt_legacy_fernet(user_id: str, provider: str, token: EncryptedToken) -> str:
    if not legacy_fernet_fallback_is_active():
        raise LegacyFernetFallbackDeprecatedError(
            "legacy Fernet token fallback deprecated after "
            f"{LEGACY_FERNET_FALLBACK_DEPRECATES_ON.isoformat()}"
        )
    try:
        payload = secret_store.decrypt(token.ciphertext)
    except Exception as exc:  # cryptography.fernet.InvalidToken et al.
        raise CiphertextCorruptedError(
            "ciphertext failed Fernet authentication"
        ) from exc
    return _load_binding_payload(payload, user_id, provider)


def _decryption_audit_ref(
    token: EncryptedToken,
    *,
    fallback_tenant_id: str,
) -> tuple[str, str, Optional[str], str]:
    """Return ``tenant_id, key_id, dek_id, provider`` for KS.1.5 audit.

    New KS.1.3 token envelopes carry the tenant DEK ref. Legacy Fernet
    rows predate the DEK schema, so the audit row records the caller's
    tenant fallback and the legacy key marker.
    """

    try:
        _, dek_ref = _load_token_envelope(token.ciphertext)
    except CiphertextCorruptedError:
        if token.ciphertext.lstrip().startswith("{"):
            raise
        return (
            fallback_tenant_id,
            "legacy-fernet",
            None,
            "local-fernet",
        )
    return (
        dek_ref.tenant_id,
        dek_ref.key_id,
        dek_ref.dek_id,
        dek_ref.provider,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def encrypt_for_user(
    user_id: str,
    provider: str,
    plaintext: str,
    *,
    tenant_id: Optional[str] = None,
    as_of: Optional[_dt.date] = None,
) -> EncryptedToken:
    """Encrypt *plaintext* (an OAuth access_token / refresh_token) for
    storage in the ``oauth_tokens`` row owned by *user_id* + *provider*.

    The plaintext is wrapped in a binding payload (see module
    docstring) before being handed to the KS.1.2 envelope helper, so
    the resulting ciphertext is bound to this *(user_id, provider)*
    pair and to a tenant DEK.

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
    tid = _tenant_id_for_user(uid, tenant_id)
    payload = _binding_payload(uid, p, tok)
    ciphertext, dek_ref = tenant_envelope.encrypt(
        payload,
        tid,
        purpose="as-token-vault",
    )
    return EncryptedToken(
        ciphertext=_token_envelope(ciphertext, dek_ref),
        key_version=current_key_version(as_of=as_of),
    )


def decrypt_for_user(
    user_id: str,
    provider: str,
    token: EncryptedToken,
    *,
    as_of: Optional[_dt.date] = None,
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
        ``token.key_version`` is outside the accepted quarterly
        master-KEK schedule.
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
    _check_key_version(token.key_version, as_of=as_of)

    try:
        ciphertext, dek_ref = _load_token_envelope(token.ciphertext)
    except CiphertextCorruptedError:
        if token.ciphertext.lstrip().startswith("{"):
            raise
        return _decrypt_legacy_fernet(uid, p, token)
    try:
        payload = tenant_envelope.decrypt(ciphertext, dek_ref)
    except tenant_envelope.BindingMismatchError as exc:
        raise BindingMismatchError(str(exc)) from exc
    except tenant_envelope.UnknownEnvelopeVersionError as exc:
        raise UnknownTokenEnvelopeVersionError(str(exc)) from exc
    except tenant_envelope.EnvelopeEncryptionError as exc:
        raise CiphertextCorruptedError(
            "ciphertext failed KS.1 envelope authentication"
        ) from exc
    return _load_binding_payload(payload, uid, p)


async def decrypt_for_user_with_audit(
    user_id: str,
    provider: str,
    token: EncryptedToken,
    *,
    tenant_id: Optional[str] = None,
    request_id: Optional[str] = None,
    actor: Optional[str] = None,
    purpose: str = "as-token-vault",
    as_of: Optional[_dt.date] = None,
) -> str:
    """Decrypt *token* and emit the KS.1.5 N10 audit row.

    The underlying decrypt remains the same binding-checked helper.
    After plaintext is recovered, this writes ``tenant_id`` /
    ``user_id`` / ledger ``ts`` / ``key_id`` / ``request_id`` into the
    tenant audit chain via :mod:`backend.security.decryption_audit`.
    """

    uid = _check_user_id(user_id)
    p = _check_provider(provider)
    fallback_tid = _tenant_id_for_user(uid, tenant_id)
    req_id = _request_id_or_new(request_id)
    plaintext = decrypt_for_user(uid, p, token, as_of=as_of)
    audit_tenant_id, key_id, dek_id, key_provider = _decryption_audit_ref(
        token,
        fallback_tenant_id=fallback_tid,
    )
    await decryption_audit.emit_decryption(
        decryption_audit.DecryptionAuditContext(
            tenant_id=audit_tenant_id,
            user_id=uid,
            key_id=key_id,
            request_id=req_id,
            purpose=purpose,
            provider=key_provider,
            actor=actor or uid,
            dek_id=dek_id,
        )
    )
    return plaintext


def decrypt_for_user_with_lazy_reencrypt(
    user_id: str,
    provider: str,
    token: EncryptedToken,
    *,
    tenant_id: Optional[str] = None,
    as_of: Optional[_dt.date] = None,
) -> DecryptedToken:
    """Decrypt and prepare a replacement when the row's KEK epoch is old.

    This is the KS.1.4 lazy re-encrypt hook. It is intentionally pure:
    callers can run it in request-time or background scans, then persist
    ``result.replacement`` with their existing optimistic-lock SQL.
    """

    plaintext = decrypt_for_user(user_id, provider, token, as_of=as_of)
    target = current_key_version(as_of=as_of)
    replacement: Optional[EncryptedToken] = None
    if token.key_version < target:
        replacement = encrypt_for_user(
            user_id,
            provider,
            plaintext,
            tenant_id=tenant_id,
            as_of=as_of,
        )
    return DecryptedToken(
        plaintext=plaintext,
        replacement=replacement,
        key_version=token.key_version,
        target_key_version=target,
    )


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
]

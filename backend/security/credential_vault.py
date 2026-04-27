"""AS.6.2 — Generalised credential vault: expand phase for git / llm secrets.

Extends the AS.2.1 :mod:`backend.security.token_vault` binding-envelope
pattern to non-OAuth credential subsystems — currently
:mod:`backend.git_credentials` (git account tokens / SSH keys / webhook
secrets, persisted in the ``git_accounts`` table by Phase 5-2~5-4) and
:mod:`backend.llm_credentials` (per-tenant LLM provider API keys,
persisted in the ``llm_credentials`` table by Phase 5b-1~5b-3).

This module is the **expand-phase** half of the AS.0.4 §2 Track C
"helper-level vault unification" plan. Per AS.0.4 §6.1 expand-phase
acceptance criteria, it is **pure additive**:

* No alembic migration.
* No change to ``git_credentials.py`` / ``llm_credentials.py`` / their
  router endpoints / their UI contracts.
* No caller wired to this module — every existing call site still
  encrypts via :func:`backend.secret_store.encrypt` directly. Adopting
  this vault is the migrate-phase row's job (Track C #2 in AS.0.4 §2).

Design intent
─────────────
The current ``git_accounts`` / ``llm_credentials`` rows store ciphertext
produced by raw :func:`secret_store.encrypt` — a Fernet token over
plaintext with no application-layer binding to the row's identity. A
DB-level row-shuffle attacker (someone able to ``UPDATE
git_accounts SET encrypted_token = (SELECT encrypted_token FROM
git_accounts WHERE id = ...)`` cross-row) would slip past Fernet's
authenticated-encryption check because the ciphertext is still valid for
the master key. AS.2.1 closed this gap for OAuth tokens via a binding
envelope; AS.6.2 generalises that defence to git + llm credentials so
every "secret-at-rest" row in OmniSight gets the same row-identity
binding when callers eventually opt in.

Cryptographic shape
───────────────────
The vault wraps each plaintext in a JSON envelope before handing it to
:func:`backend.secret_store.encrypt` (the project-wide Fernet cipher,
the same one ``token_vault`` / ``git_credentials`` / ``llm_credentials``
/ ``codesign_store`` already use)::

    {
      "fmt": 1,                       # AS.6.2 envelope format version
      "rec": "git_token" | "git_ssh_key" | "git_webhook_secret"
              | "llm_value",          # record-type discriminator
      "salt": "<b64 16 bytes>",       # per-row random nonce-tag
      "tid": "<tenant_id>",           # row's tenant scope
      "rid": "<resource id>",         # account_id / credential_id
      "tok": "<plaintext>"            # the secret material
    }

On decrypt the envelope is parsed inside the Fernet authenticated
boundary; ``rec`` MUST equal the record-type the caller is asking for,
``tid`` MUST match the claimed tenant, and ``rid`` MUST match the
claimed resource id. Otherwise :class:`BindingMismatchError` is raised.

Why a separate envelope shape from :mod:`token_vault`
─────────────────────────────────────────────────────
``token_vault``'s envelope is keyed on ``uid`` (user_id) + ``prv``
(provider) — appropriate for OAuth tokens, which are owned by an
end-user and scoped to a single IdP. git/llm credentials are owned by a
*tenant* and can have multiple kinds of secret in one row (the same
``git_accounts`` row carries both an access token AND an SSH key AND a
webhook secret). The envelope therefore needs a distinct ``rec``
discriminator + a tenant scope. Cross-decrypting a ``token_vault``
ciphertext through this module — or vice versa — fails at envelope-
shape validation, which is the correct security outcome (each module
owns its own binding contract).

Single-master-key invariant (AS.0.4 §3.1, hard cross-phase rule)
────────────────────────────────────────────────────────────────
``secret_store._fernet`` is the **only** Fernet key this vault uses —
the same key ``token_vault`` / ``git_credentials`` / ``llm_credentials``
/ ``codesign_store`` already share. The vault MUST NOT mint a second
master key, MUST NOT read a separate OAuth / git / llm secret-key env
var, and MUST NOT derive a per-row sub-key (no key-derivation
function). The per-row salt is part of the binding envelope, not a
key derivation input. This mirrors the AS.2.1 token-vault contract
byte-for-byte and is enforced by a grep-based drift guard test
(see ``test_credential_vault_uses_secret_store_fernet``).

``key_version`` reservation
───────────────────────────
``key_version`` is reserved for the future KMS rotation hook
(AS.0.4 §3.1 #4). All ciphertext written today is at
:data:`KEY_VERSION_CURRENT` = 1. Anything else on read raises
:class:`UnknownKeyVersionError`. This matches token_vault's stance and
keeps the multi-version dual-read flow a single coordinated landing
across both vaults.

Migrate-phase plan (NOT executed in this row)
─────────────────────────────────────────────
A future row will perform Track C migrate-phase for each subsystem
independently:

1. **git_credentials migrate**:
   * Dual-write: every ``encrypted_token`` / ``encrypted_ssh_key`` /
     ``encrypted_webhook_secret`` write goes through both the legacy
     :func:`secret_store.encrypt` path (unchanged column) AND
     :func:`encrypt_git_secret` (new sibling column / JSONB envelope —
     schema choice deferred to that row).
   * Dual-read: callers prefer the binding-checked decrypt; on miss
     fall back to the legacy ciphertext.
   * Keep both for ≥ 14 days observation.
2. **llm_credentials migrate**: same pattern, keyed on
   ``credential_id``.
3. **Contract phase**: drop the legacy column, drop the legacy
   resolver branch, audit_log the cutover. Per AS.0.4 §2 Track C #3
   "嚴禁三表同時 contract" — one subsystem at a time.

The :func:`migrate_legacy_secret_store_ciphertext` helper here is the
forward-reservation seam for that work — it decrypts a legacy
``secret_store.encrypt``-only ciphertext (no binding envelope) and
re-wraps it through the binding envelope, returning an
:class:`EncryptedSecret` ready for the new column. No caller invokes
it yet; it ships green so the migrate-phase row can wire it without
re-deriving the helper interface.

AS.0.8 single-knob hook
───────────────────────
:func:`is_enabled` reads ``settings.as_enabled`` via ``getattr``
fallback (forward-promotion guard, mirrors AS.2.1 / AS.1.1 / AS.0.9).
The pure helpers (:func:`encrypt_git_secret`, :func:`decrypt_git_secret`,
:func:`encrypt_llm_credential`, :func:`decrypt_llm_credential`) do
**NOT** auto-gate — backfill / DSAR / key-rotation paths must keep
working knob-off, same invariant token_vault honours.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* No module-level mutable state — only frozen dataclasses, immutable
  tuples / strings, ``frozenset``s.
* Randomness comes from :mod:`secrets` (kernel CSPRNG).
* Fernet key fetch delegated to :mod:`backend.secret_store`, which owns
  the ``fcntl.flock`` first-boot generation guard (task #104). Each
  uvicorn worker reads the same on-disk key so cross-worker ciphertext
  decrypts uniformly — SOP §1 answer #2 (PG / disk coordination).
* No DB / network IO. Persistence is the caller's job.
* Importing the module is free of side effects.

Read-after-write audit (per SOP §1)
───────────────────────────────────
This row adds zero write paths; the migrate-phase row will introduce
dual-write semantics with the read-after-write hazard discussed in
AS.0.4 §4.3 (dual-write loser handling). Until then the seam is
read-only by inspection.

Path note
─────────
Sibling to :mod:`backend.security.token_vault` — same per-AS path
deviation rationale (AS.1.1 / AS.1.3 / AS.1.4 / AS.0.10 precedent):
``backend/auth/credential_vault.py`` is the canonical AS namespace but
``backend/auth.py`` legacy session/RBAC blocks promotion to a package.
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

#: Active key version. AS.0.4 §3.1 reserves this for future KMS
#: rotations; the first rotation row will land a v2 branch + a dual-read
#: fallback. Any other value on read raises
#: :class:`UnknownKeyVersionError`. Mirrors
#: :data:`backend.security.token_vault.KEY_VERSION_CURRENT` byte-for-byte.
KEY_VERSION_CURRENT: int = 1

#: AS.6.2 binding envelope shape version. Distinct from token_vault's
#: :data:`backend.security.token_vault.BINDING_FORMAT_VERSION` because
#: this envelope carries different fields (``rec`` / ``tid`` / ``rid``
#: instead of ``uid`` / ``prv``); the integer happens to be 1 for both,
#: but a future bump on either side is independent.
BINDING_FORMAT_VERSION: int = 1

#: Per-row salt size (bytes). 16 bytes / 128 bits — same value
#: token_vault uses; the salt is part of the auth-checked envelope, not
#: a key derivation input.
_SALT_RAW_BYTES: int = 16

#: Record-type discriminator strings — pinned for the AS.6.2 row's
#: scope. New record types (e.g. ``codesign_record``) need their own
#: row + drift guard updates per AS.0.4 §2 Track C #3.
RECORD_GIT_TOKEN: str = "git_token"
RECORD_GIT_SSH_KEY: str = "git_ssh_key"
RECORD_GIT_WEBHOOK_SECRET: str = "git_webhook_secret"
RECORD_LLM_VALUE: str = "llm_value"

#: Whitelist of accepted record-type strings. Caller-supplied values
#: are validated against this set before any crypto work happens, so a
#: typo on encrypt cannot silently produce a row no decrypter would
#: recognise.
SUPPORTED_RECORD_TYPES: frozenset[str] = frozenset(
    {
        RECORD_GIT_TOKEN,
        RECORD_GIT_SSH_KEY,
        RECORD_GIT_WEBHOOK_SECRET,
        RECORD_LLM_VALUE,
    }
)

#: Subset of record types that belong to ``git_accounts`` rows. Used by
#: :func:`encrypt_git_secret` / :func:`decrypt_git_secret` to constrain
#: the ``secret_kind`` parameter — the LLM row helpers cannot
#: accidentally accept a git record type and vice versa.
SUPPORTED_GIT_RECORD_TYPES: frozenset[str] = frozenset(
    {
        RECORD_GIT_TOKEN,
        RECORD_GIT_SSH_KEY,
        RECORD_GIT_WEBHOOK_SECRET,
    }
)

#: Subset of record types that belong to ``llm_credentials`` rows.
SUPPORTED_LLM_RECORD_TYPES: frozenset[str] = frozenset(
    {
        RECORD_LLM_VALUE,
    }
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class CredentialVaultError(Exception):
    """Base class for credential-vault errors. Lets callers
    ``except CredentialVaultError`` once instead of enumerating each
    subclass."""


class UnsupportedRecordTypeError(CredentialVaultError, ValueError):
    """``record_type`` is not in :data:`SUPPORTED_RECORD_TYPES`, or the
    domain helper was passed a record type belonging to a different
    domain (e.g. ``RECORD_LLM_VALUE`` to :func:`encrypt_git_secret`).
    Subclasses ``ValueError`` so input-validation ``except ValueError``
    callers continue to work."""


class UnknownKeyVersionError(CredentialVaultError):
    """``key_version`` on a stored row is not :data:`KEY_VERSION_CURRENT`.
    The first KMS migration will replace this with a multi-version
    dispatch; until then any unknown value is treated as corruption."""


class BindingMismatchError(CredentialVaultError):
    """The decrypted binding envelope's ``rec`` / ``tid`` / ``rid`` did
    not match the values the caller claimed the row belongs to.
    Indicates either a DB-level row shuffle or a caller bug passing the
    wrong tenant / resource id on decrypt."""


class CiphertextCorruptedError(CredentialVaultError):
    """The ciphertext could not be decrypted (Fernet auth failed) or
    the inner JSON envelope was malformed. Re-raised as a vault-layer
    error so callers don't need to special-case the underlying Fernet
    InvalidToken exception."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Frozen dataclasses (public surface)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class EncryptedSecret:
    """Persistent shape for one stored credential secret.

    Round-tripped to / from a row's ``encrypted_*`` (TEXT) column +
    a sibling ``key_version`` column (the migrate-phase row will
    decide whether ``key_version`` lives in the existing
    ``git_accounts`` / ``llm_credentials`` schema or in a sibling
    JSONB field — out of scope for the expand-only AS.6.2 row).

    The ``ciphertext`` is the Fernet token (urlsafe-base64 ASCII),
    and ``key_version`` MUST be :data:`KEY_VERSION_CURRENT` for this
    release. The salt lives inside the Fernet-authenticated envelope,
    not as a public attribute — same invariant as
    :class:`backend.security.token_vault.EncryptedToken`.
    """

    ciphertext: str
    key_version: int


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AS.0.8 single-knob hook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def is_enabled() -> bool:
    """Whether the AS feature family is enabled per AS.0.8 §3.1.

    Reads ``settings.as_enabled`` via ``getattr`` fallback (default
    ``True``) so the module loads cleanly before AS.3.1 lands the field
    on :class:`backend.config.Settings` — same forward-promotion pattern
    AS.2.1 / AS.1.1 / AS.0.9 use.

    The vault's pure helpers do NOT auto-call this hook (per module
    docstring, backfill / DSAR / key-rotation paths must keep working
    knob-off). Caller endpoints gate on it before reaching the vault.
    """
    try:
        from backend.config import settings
    except Exception:  # pragma: no cover — config import never fails in prod
        return True
    return bool(getattr(settings, "as_enabled", True))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internal helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _check_record_type(record_type: str, *, allowed: frozenset[str]) -> str:
    if not isinstance(record_type, str) or not record_type:
        raise UnsupportedRecordTypeError(
            f"record_type must be a non-empty string, got "
            f"{type(record_type).__name__}"
        )
    if record_type not in allowed:
        raise UnsupportedRecordTypeError(
            f"unsupported record_type: {record_type!r} "
            f"(expected one of {sorted(allowed)})"
        )
    return record_type


def _check_id_field(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CredentialVaultError(
            f"{field_name} must be a non-empty string, got "
            f"{type(value).__name__}"
        )
    return value


def _check_plaintext(plaintext: str) -> str:
    if not isinstance(plaintext, str):
        raise CredentialVaultError(
            f"plaintext must be a string, got {type(plaintext).__name__}"
        )
    if not plaintext:
        # Empty string would survive Fernet round-trip but defeats the
        # purpose of the vault. Callers that intentionally store an
        # absent secret (e.g. an SSH-only git account row with no
        # webhook_secret) should NOT call the vault at all — the row's
        # column simply stays NULL / "".
        raise CredentialVaultError("plaintext must not be empty")
    return plaintext


def _b64_salt() -> tuple[bytes, str]:
    raw = secrets.token_bytes(_SALT_RAW_BYTES)
    return raw, base64.b64encode(raw).decode("ascii")


def _encrypt(
    *,
    record_type: str,
    tenant_id: str,
    resource_id: str,
    plaintext: str,
) -> EncryptedSecret:
    """Internal binding-envelope encrypt. Domain-public wrappers
    pre-validate the record type against their narrower whitelist."""

    rec = _check_record_type(record_type, allowed=SUPPORTED_RECORD_TYPES)
    tid = _check_id_field(tenant_id, "tenant_id")
    rid = _check_id_field(resource_id, "resource_id")
    tok = _check_plaintext(plaintext)
    _, salt_b64 = _b64_salt()

    envelope = {
        "fmt": BINDING_FORMAT_VERSION,
        "rec": rec,
        "salt": salt_b64,
        "tid": tid,
        "rid": rid,
        "tok": tok,
    }
    payload = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    ciphertext = secret_store.encrypt(payload)
    return EncryptedSecret(
        ciphertext=ciphertext,
        key_version=KEY_VERSION_CURRENT,
    )


def _decrypt(
    *,
    record_type: str,
    tenant_id: str,
    resource_id: str,
    secret: EncryptedSecret,
) -> str:
    """Internal binding-envelope decrypt + validation. Domain-public
    wrappers pre-validate the record type against their narrower
    whitelist."""

    rec = _check_record_type(record_type, allowed=SUPPORTED_RECORD_TYPES)
    tid = _check_id_field(tenant_id, "tenant_id")
    rid = _check_id_field(resource_id, "resource_id")
    if not isinstance(secret, EncryptedSecret):
        raise CredentialVaultError(
            f"secret must be an EncryptedSecret, got {type(secret).__name__}"
        )
    if secret.key_version != KEY_VERSION_CURRENT:
        raise UnknownKeyVersionError(
            f"unknown key_version={secret.key_version!r} "
            f"(this release supports only {KEY_VERSION_CURRENT})"
        )

    try:
        payload = secret_store.decrypt(secret.ciphertext)
    except Exception as exc:
        raise CiphertextCorruptedError(
            "ciphertext failed Fernet authentication"
        ) from exc

    try:
        envelope = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise CiphertextCorruptedError(
            "inner envelope is not valid JSON"
        ) from exc
    if not isinstance(envelope, dict):
        raise CiphertextCorruptedError(
            f"inner envelope must be an object, got {type(envelope).__name__}"
        )

    fmt = envelope.get("fmt")
    if fmt != BINDING_FORMAT_VERSION:
        # Fernet auth passed; the ciphertext just isn't an AS.6.2
        # envelope this release understands. Treat as binding mismatch
        # (the future migrate row may want to special-case fmt=0 for
        # legacy ciphertext — that's :func:`migrate_legacy_secret_store_ciphertext`'s
        # job, NOT decrypt's).
        raise BindingMismatchError(
            f"unknown binding format version: {fmt!r} "
            f"(this release supports only {BINDING_FORMAT_VERSION})"
        )

    stored_rec = envelope.get("rec")
    stored_tid = envelope.get("tid")
    stored_rid = envelope.get("rid")
    if (
        not isinstance(stored_rec, str)
        or not isinstance(stored_tid, str)
        or not isinstance(stored_rid, str)
    ):
        raise CiphertextCorruptedError(
            "envelope missing 'rec' / 'tid' / 'rid' string fields"
        )

    if not hmac.compare_digest(stored_rec, rec):
        raise BindingMismatchError(
            "ciphertext bound to a different record_type"
        )
    if not hmac.compare_digest(stored_tid, tid):
        raise BindingMismatchError(
            "ciphertext bound to a different tenant_id"
        )
    if not hmac.compare_digest(stored_rid, rid):
        raise BindingMismatchError(
            "ciphertext bound to a different resource_id"
        )

    plaintext = envelope.get("tok")
    if not isinstance(plaintext, str):
        raise CiphertextCorruptedError(
            "envelope 'tok' field missing or not a string"
        )
    return plaintext


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API — git_accounts
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def encrypt_git_secret(
    *,
    account_id: str,
    tenant_id: str,
    secret_kind: str,
    plaintext: str,
) -> EncryptedSecret:
    """Encrypt *plaintext* for storage in a ``git_accounts`` row.

    ``secret_kind`` selects which column the ciphertext is destined
    for: :data:`RECORD_GIT_TOKEN` (access token / PAT),
    :data:`RECORD_GIT_SSH_KEY` (SSH private key material), or
    :data:`RECORD_GIT_WEBHOOK_SECRET` (HMAC webhook secret).

    The resulting ciphertext is bound to *(secret_kind, tenant_id,
    account_id)*: a row swap or column swap in the database (e.g.
    copying one account's webhook_secret into another's, or a token's
    ciphertext into the ssh_key column) is caught at decrypt time.

    No caller invokes this in the AS.6.2 expand-phase row — the
    ``git_credentials.py`` write paths still call
    :func:`backend.secret_store.encrypt` directly. The migrate-phase
    row will dual-write through this helper.

    Raises
    ------
    UnsupportedRecordTypeError
        ``secret_kind`` is not in :data:`SUPPORTED_GIT_RECORD_TYPES`.
    CredentialVaultError
        ``account_id`` / ``tenant_id`` / ``plaintext`` is missing /
        not a string.
    """
    _check_record_type(secret_kind, allowed=SUPPORTED_GIT_RECORD_TYPES)
    return _encrypt(
        record_type=secret_kind,
        tenant_id=tenant_id,
        resource_id=account_id,
        plaintext=plaintext,
    )


def decrypt_git_secret(
    *,
    account_id: str,
    tenant_id: str,
    secret_kind: str,
    secret: EncryptedSecret,
) -> str:
    """Decrypt *secret* and return its plaintext, verifying the
    binding envelope matches the *(secret_kind, tenant_id, account_id)*
    the caller is claiming the row belongs to.

    Raises
    ------
    UnsupportedRecordTypeError
        ``secret_kind`` is not in :data:`SUPPORTED_GIT_RECORD_TYPES`.
    UnknownKeyVersionError
        ``secret.key_version`` is not :data:`KEY_VERSION_CURRENT`.
    BindingMismatchError
        The ciphertext was encrypted for a different
        *(secret_kind, tenant_id, account_id)* triple, or for a
        different binding-format version.
    CiphertextCorruptedError
        Fernet authentication failed, or the inner JSON envelope is
        malformed.
    """
    _check_record_type(secret_kind, allowed=SUPPORTED_GIT_RECORD_TYPES)
    return _decrypt(
        record_type=secret_kind,
        tenant_id=tenant_id,
        resource_id=account_id,
        secret=secret,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public API — llm_credentials
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def encrypt_llm_credential(
    *,
    credential_id: str,
    tenant_id: str,
    plaintext: str,
) -> EncryptedSecret:
    """Encrypt *plaintext* (an LLM provider API key) for storage in an
    ``llm_credentials`` row.

    The resulting ciphertext is bound to
    *(:data:`RECORD_LLM_VALUE`, tenant_id, credential_id)*.

    Raises
    ------
    CredentialVaultError
        ``credential_id`` / ``tenant_id`` / ``plaintext`` is missing /
        not a string.
    """
    return _encrypt(
        record_type=RECORD_LLM_VALUE,
        tenant_id=tenant_id,
        resource_id=credential_id,
        plaintext=plaintext,
    )


def decrypt_llm_credential(
    *,
    credential_id: str,
    tenant_id: str,
    secret: EncryptedSecret,
) -> str:
    """Decrypt *secret* and return its plaintext, verifying the
    binding envelope matches the
    *(:data:`RECORD_LLM_VALUE`, tenant_id, credential_id)* triple.

    Raises
    ------
    UnknownKeyVersionError
        ``secret.key_version`` is not :data:`KEY_VERSION_CURRENT`.
    BindingMismatchError
        The ciphertext was encrypted for a different *(tenant_id,
        credential_id)* pair, or for a different binding-format
        version, or for a non-LLM record type.
    CiphertextCorruptedError
        Fernet authentication failed, or the inner JSON envelope is
        malformed.
    """
    return _decrypt(
        record_type=RECORD_LLM_VALUE,
        tenant_id=tenant_id,
        resource_id=credential_id,
        secret=secret,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Forward-reservation: migrate-phase helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def migrate_legacy_secret_store_ciphertext(
    *,
    record_type: str,
    tenant_id: str,
    resource_id: str,
    legacy_ciphertext: str,
) -> Optional[EncryptedSecret]:
    """Re-wrap a legacy :func:`backend.secret_store.encrypt`-only
    ciphertext into an AS.6.2 binding envelope.

    Forward-reservation seam for the migrate-phase row of AS.0.4 §2
    Track C. The migrate row is expected to scan ``git_accounts`` /
    ``llm_credentials`` and call this helper per row, persisting the
    returned :class:`EncryptedSecret` to a new sibling column (or
    JSONB field — schema choice deferred to that row).

    Behaviour
    ─────────
    * Empty / non-string ``legacy_ciphertext`` returns ``None`` —
      callers can use this to skip rows whose secret is unset (e.g.
      a git_accounts row with ``encrypted_ssh_key=''``).
    * Fernet decryption failure raises :class:`CiphertextCorruptedError`.
      The migrate row should record-and-skip such rows for operator
      attention rather than treating them as fatal.
    * On success, the returned :class:`EncryptedSecret` is the same
      shape :func:`encrypt_git_secret` / :func:`encrypt_llm_credential`
      produce; the migrate row writes it to the new column verbatim.

    Why this lives here, not in ``secret_store``
    ─────────────────────────────────────────────
    The legacy column has no record-type tag; only the *call site*
    knows whether it's a git token or an LLM value. Moving the
    rewrap into ``secret_store`` would require leaking AS-record-type
    vocabulary into the project-wide cipher module — wrong layer.
    Migrate-phase callers pass record_type explicitly.

    The AS.6.2 row ships this helper green but does NOT call it. The
    migrate row will.

    Raises
    ------
    UnsupportedRecordTypeError
        ``record_type`` is not in :data:`SUPPORTED_RECORD_TYPES`.
    CiphertextCorruptedError
        Fernet authentication on ``legacy_ciphertext`` failed.
    """
    _check_record_type(record_type, allowed=SUPPORTED_RECORD_TYPES)
    if not isinstance(legacy_ciphertext, str) or not legacy_ciphertext:
        return None
    try:
        plaintext = secret_store.decrypt(legacy_ciphertext)
    except Exception as exc:
        raise CiphertextCorruptedError(
            "legacy ciphertext failed Fernet authentication"
        ) from exc
    if not plaintext:
        # Legacy column round-trips empty plaintext fine but the new
        # vault rejects empty (defensible signal: nothing worth
        # protecting). Mirror skip semantics.
        return None
    return _encrypt(
        record_type=record_type,
        tenant_id=tenant_id,
        resource_id=resource_id,
        plaintext=plaintext,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Convenience re-export
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def fingerprint(token: str) -> str:
    """Last-4-character fingerprint for UI / log surfacing.

    Thin re-export of :func:`backend.secret_store.fingerprint` so vault
    callers don't need to import a second module just to redact a
    secret in a structured log line. Mirrors token_vault's re-export.
    """
    return secret_store.fingerprint(token or "")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


__all__ = [
    "BINDING_FORMAT_VERSION",
    "BindingMismatchError",
    "CiphertextCorruptedError",
    "CredentialVaultError",
    "EncryptedSecret",
    "KEY_VERSION_CURRENT",
    "RECORD_GIT_SSH_KEY",
    "RECORD_GIT_TOKEN",
    "RECORD_GIT_WEBHOOK_SECRET",
    "RECORD_LLM_VALUE",
    "SUPPORTED_GIT_RECORD_TYPES",
    "SUPPORTED_LLM_RECORD_TYPES",
    "SUPPORTED_RECORD_TYPES",
    "UnknownKeyVersionError",
    "UnsupportedRecordTypeError",
    "decrypt_git_secret",
    "decrypt_llm_credential",
    "encrypt_git_secret",
    "encrypt_llm_credential",
    "fingerprint",
    "is_enabled",
    "migrate_legacy_secret_store_ciphertext",
]

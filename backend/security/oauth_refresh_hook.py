"""AS.2.4 — OAuth access-token refresh hook.

Stateless orchestrator that drives the "auto-refresh access tokens
within ``skew_seconds`` of expiry" lifecycle on a single ``oauth_tokens``
row (the per-user / per-provider credential AS.2.2 lays out).  The
caller passes in:

* a :class:`TokenVaultRecord` snapshot of the row's encrypted columns,
* a ``refresh_fn`` async callable that talks to the IdP's token
  endpoint with ``grant_type=refresh_token``,

and the hook returns a :class:`RefreshOutcome` carrying

* ``outcome`` — one of :data:`OUTCOME_*` (locked vocabulary),
* ``new_record`` — a freshly-encrypted :class:`TokenVaultRecord`
  ready for an UPDATE-with-optimistic-lock (``version + 1``) on the
  ``oauth_tokens`` row,
* ``rotated`` — boolean from :func:`oauth_client.apply_rotation`
  signalling whether the IdP issued a brand-new refresh_token (RFC
  6749 §10.4 / OAuth 2.1 BCP §4.13 rotation).

The hook does NOT touch the database.  The persistence half — selecting
due rows, executing the UPDATE, retrying on optimistic-lock conflicts —
is the caller's job (AS.6.1 OAuth router for request-time lazy refresh,
or a future scheduled-task row for proactive background scanning).  This
keeps the hook composable across both PG (asyncpg) and SQLite
(aiosqlite) without dialect-specific code, and lets the unit tests run
without any DB shim.

What this row delivers (TODO line "Refresh hook：access token 過期前
60s 自動 refresh")
─────────────────────────────────────────────────────────────────────

* :func:`is_due` — predicate that mirrors :meth:`oauth_client.TokenSet.needs_refresh`
  but operates on a stored :class:`TokenVaultRecord` instead of an
  in-memory :class:`~oauth_client.TokenSet`.
* :func:`refresh_record` — the actual hook.  Decrypts via
  :mod:`backend.security.token_vault`, calls the caller-provided
  ``refresh_fn``, re-applies RFC 6749 §10.4 rotation via
  :func:`oauth_client.apply_rotation`, re-encrypts, and emits the
  AS.1.4 ``oauth.refresh`` + (if rotated) ``oauth.token_rotated``
  audit rows.  Honours the AS.0.8 single-knob via the audit layer's
  silent-skip (knob-off ⇒ refresh still runs but no audit row is
  written, mirroring the pure-helpers-stay-callable convention every
  AS module ships).

Why pure helpers, not a scheduler
─────────────────────────────────
1. **Composition** — AS.6.1 will likely call this hook *lazily* on
   each provider API request, not from a background loop, because
   provider rate limits + cold-start cost make eager refresh a poor
   trade.  Keeping the hook stateless lets that wiring be a one-liner.
2. **Testability** — no DB / network mocks needed.  Tests pin behaviour
   on ``RefreshOutcome`` shape across a fake :class:`TokenVaultRecord`
   + a fake async ``refresh_fn``.
3. **AS.0.4 §6.2 compliance** — backfill / DSAR / key-rotation paths
   must keep working with the AS knob off.  Pure helpers don't gate;
   only audit emitters do (and they silent-skip, so the helper itself
   is callable regardless).

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* No module-level mutable state.  Two frozen dataclasses
  (:class:`TokenVaultRecord`, :class:`RefreshOutcome`), four immutable
  ``OUTCOME_*`` strings, one tuple of those strings.
* ``RefreshHookError`` subclass tree.  No DB connections, no env
  reads, no caches.  Importing the module is side-effect free.
* All randomness comes from the vault (which itself comes from
  :mod:`secrets`).  No ``random``, no ``time.time`` at module top.
* Cross-worker consistency: the only shared state is the
  ``oauth_tokens`` row itself + the audit chain.  Per-row optimistic
  lock (``version`` column) protects against two workers refreshing
  the same row simultaneously — answer #1 of SOP §1 (every worker
  reads same DB state); the loser sees ``rowcount=0`` from its UPDATE
  and bails out.

Read-after-write timing audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────────
The hook itself only mutates in-memory data.  The DB read-then-write
race lives in the caller's UPDATE; the AS.2.2 ``version`` column is
the optimistic-lock counter that closes that race.  Two workers that
both ``SELECT`` the same expiring row will both compute a refresh
attempt; the first to ``UPDATE WHERE version = N`` lands and bumps
to ``N+1``; the second's ``UPDATE WHERE version = N`` returns
``rowcount = 0`` and the caller retries (or skips, since the row is
now fresh).  See :class:`RefreshHookError` subclass tree for the
typed signal back to the caller.

TS twin (forward note)
──────────────────────
``templates/_shared/oauth-client/index.ts`` already ships
:class:`AutoRefreshFetch` / ``autoRefresh`` for *per-request* lazy
refresh in generated apps — the in-process equivalent of AS.1.1's
:class:`oauth_client.AutoRefreshAuth`.  The TS side does not need a
"scan oauth_tokens table" twin because generated apps do not own a
server-side token store; their tokens live in caller-managed
keystore (IndexedDB / mobile vendor secure-storage) and are
refreshed at use-time.  The Python-only refresh hook is the
server-side mirror that AS.2.2's ``oauth_tokens`` table requires.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional

from backend.security import oauth_audit, oauth_client, token_vault
from backend.security.oauth_client import (
    DEFAULT_REFRESH_SKEW_SECONDS,
    TokenRefreshError,
    TokenResponseError,
    TokenSet,
)
from backend.security.token_vault import EncryptedToken, TokenVaultError

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Outcome vocabulary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

#: The refresh succeeded: ``new_record`` carries fresh ciphertext +
#: bumped ``version``; the IdP's token endpoint returned a valid
#: response.  Caller persists ``new_record`` via UPDATE-with-version
#: optimistic lock.
OUTCOME_SUCCESS = oauth_audit.OUTCOME_SUCCESS  # "success"

#: The row's ``expires_at`` is still outside the skew window (or the
#: column is NULL — provider issues no expiry hint).  No refresh was
#: attempted; ``new_record`` is ``None``.  Audit row is intentionally
#: NOT emitted (no event happened) — same pattern as
#: :meth:`oauth_client.TokenSet.needs_refresh` returning False.
OUTCOME_NOT_DUE = "not_due"

#: The row is due but ``refresh_token_enc`` is empty / absent (e.g.
#: Apple sign-in non-first-time logins, Notion workspace tokens).
#: Caller must mark the row stale and re-prompt the user; audit
#: surfaces ``oauth.refresh`` outcome=``no_refresh_token`` so ops can
#: track these.
OUTCOME_NO_REFRESH_TOKEN = oauth_audit.OUTCOME_NO_REFRESH_TOKEN  # "no_refresh_token"

#: Either ``refresh_fn`` raised, or the IdP returned a malformed /
#: error-shaped token response (RFC 6749 §5.2).  Audit row carries
#: ``error`` field with the provider-side detail.  Caller decides
#: whether to retry (transient) or invalidate the row (terminal).
OUTCOME_PROVIDER_ERROR = oauth_audit.OUTCOME_PROVIDER_ERROR  # "provider_error"

#: The vault could not decrypt one of the row's ciphertext columns —
#: either the key has rotated past the row's ``key_version`` (returns
#: :class:`token_vault.UnknownKeyVersionError`), the row was bound to
#: a different ``(user_id, provider)`` (returns
#: :class:`token_vault.BindingMismatchError` — rare, a DB row swap
#: would have to happen first), or the ciphertext was corrupted
#: (Fernet auth tag failed — :class:`token_vault.CiphertextCorruptedError`).
#: Audit emits as ``provider_error`` with an ``error="vault_*"`` prefix
#: so ops can grep these distinct from upstream IdP failures.
OUTCOME_VAULT_FAILURE = "vault_failure"

#: Ordered tuple of every outcome the hook ever surfaces.  Used by
#: callers (and tests) that need the canonical vocabulary without
#: importing each constant.
ALL_OUTCOMES: tuple[str, ...] = (
    OUTCOME_SUCCESS,
    OUTCOME_NOT_DUE,
    OUTCOME_NO_REFRESH_TOKEN,
    OUTCOME_PROVIDER_ERROR,
    OUTCOME_VAULT_FAILURE,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class RefreshHookError(Exception):
    """Base class for hook-layer errors callers may catch in bulk.
    The hook prefers returning a typed :class:`RefreshOutcome` over
    raising — the only path that raises is malformed inputs (caller
    bug) or ``trigger`` outside :data:`oauth_audit.ROTATION_TRIGGERS`.
    """


class InvalidTriggerError(RefreshHookError, ValueError):
    """``trigger`` is not in :data:`oauth_audit.ROTATION_TRIGGERS`.
    Subclasses :class:`ValueError` so existing input-validation
    ``except ValueError`` blocks continue to work."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Type aliases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

#: Caller-provided async callable that POSTs ``grant_type=refresh_token``
#: to the IdP's token endpoint and returns the parsed JSON payload.
#: Same shape as :data:`oauth_client.RefreshCallable`.
RefreshCallable = Callable[[str], Awaitable[Mapping[str, Any]]]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Frozen dataclasses (public surface)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class TokenVaultRecord:
    """Snapshot of one ``oauth_tokens`` row, encrypted side.

    Mirrors the column shape AS.2.2's alembic 0057 lays down:

    * ``access_token_enc`` is always present (may equal an empty
      ciphertext on a freshly-INSERT-ed row, but the hook never reads
      such rows — it only touches rows whose ``expires_at`` is set).
    * ``refresh_token_enc`` is ``None`` when the column was ``''``
      (the AS.2.2 default for "this provider didn't issue a refresh
      token").  Use :meth:`from_db_row` to convert a raw column
      value: it converts ``''`` → ``None`` and a non-empty string
      into an :class:`EncryptedToken` carrying the row's
      ``key_version``.
    * ``scope`` is a tuple of the granted-scope strings (caller
      splits / normalises before calling the hook); ``()`` is fine.
    * ``version`` is the AS.2.2 optimistic-lock counter; the hook
      bumps it by exactly one in :class:`RefreshOutcome.new_record`
      so the caller's ``UPDATE ... WHERE version = old_version``
      survives concurrent refreshers.
    """

    user_id: str
    provider: str
    access_token_enc: EncryptedToken
    refresh_token_enc: Optional[EncryptedToken]
    expires_at: Optional[float]
    scope: tuple[str, ...]
    version: int

    @classmethod
    def from_db_row(
        cls,
        *,
        user_id: str,
        provider: str,
        access_token_enc: str,
        refresh_token_enc: str,
        expires_at: Optional[float],
        scope: str,
        key_version: int,
        version: int,
    ) -> "TokenVaultRecord":
        """Build a record from the raw column values of one
        ``oauth_tokens`` row.

        Handles the AS.2.2 "empty string means absent" convention for
        ``refresh_token_enc``: ``''`` → ``None`` instead of an empty
        :class:`EncryptedToken`.  ``access_token_enc`` is always
        materialised — the hook is never invoked on a row whose
        access ciphertext is missing (such rows have no ``expires_at``
        either, so :func:`is_due` returns False).
        """

        access = EncryptedToken(
            ciphertext=access_token_enc,
            key_version=key_version,
        )
        if refresh_token_enc:
            refresh: Optional[EncryptedToken] = EncryptedToken(
                ciphertext=refresh_token_enc,
                key_version=key_version,
            )
        else:
            refresh = None
        scope_tuple = tuple(
            s for s in (scope or "").replace(",", " ").split() if s
        )
        return cls(
            user_id=user_id,
            provider=provider,
            access_token_enc=access,
            refresh_token_enc=refresh,
            expires_at=expires_at,
            scope=scope_tuple,
            version=version,
        )


@dataclass(frozen=True)
class RefreshOutcome:
    """Result of one :func:`refresh_record` call.

    For ``outcome == OUTCOME_SUCCESS``:

    * ``new_record`` carries the freshly-encrypted ciphertext +
      ``version + 1``; the caller persists it.
    * ``rotated`` is True iff the IdP issued a fresh ``refresh_token``
      (RFC 6749 §10.4 / OAuth 2.1 BCP §4.13).  Caller has already
      received an :data:`oauth_client.EVENT_OAUTH_TOKEN_ROTATED` audit
      row — no extra emit needed.
    * ``new_expires_in_seconds`` is the relative TTL of the new
      access_token, mirroring the IdP's ``expires_in`` (or ``None``
      if the IdP didn't include it — same null convention as
      :class:`oauth_client.TokenSet`).

    For any other outcome ``new_record`` is ``None``.  ``rotated`` is
    always False outside SUCCESS.  ``error`` carries a short string
    explaining the failure mode (vault error class name, IdP error
    field, etc.).
    """

    outcome: str
    new_record: Optional[TokenVaultRecord]
    rotated: bool
    error: Optional[str]
    previous_expires_at: Optional[float]
    new_expires_in_seconds: Optional[int]
    granted_scope: tuple[str, ...]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AS.0.8 single-knob hook (re-export for symmetry)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def is_enabled() -> bool:
    """Whether the AS feature family is enabled per AS.0.8 §3.1.

    Thin re-export of :func:`oauth_client.is_enabled` so callers can
    gate their *invocation* of the hook (e.g. a scheduled scanner
    that should not even start when the knob is off).  The hook's
    internal pure helpers do NOT call this — they delegate to the
    audit layer's :func:`oauth_audit._gate` for the silent-skip
    behaviour AS.0.4 §6.2 mandates.
    """
    return oauth_client.is_enabled()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Predicate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def is_due(
    record: TokenVaultRecord,
    *,
    skew_seconds: int = DEFAULT_REFRESH_SKEW_SECONDS,
    now: Optional[float] = None,
) -> bool:
    """Whether *record* is within ``skew_seconds`` of expiry.

    Returns False when ``record.expires_at`` is ``None`` (provider
    didn't issue an expiry hint — caller must rely on a 401 response
    instead).  Mirrors :meth:`oauth_client.TokenSet.needs_refresh`
    semantics exactly so the two predicates agree on edge cases (the
    skew-second threshold is inclusive, just like ``needs_refresh``).
    """
    if record.expires_at is None:
        return False
    ts = time.time() if now is None else now
    return ts >= (record.expires_at - skew_seconds)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Orchestrator — the hook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def refresh_record(
    record: TokenVaultRecord,
    refresh_fn: RefreshCallable,
    *,
    skew_seconds: int = DEFAULT_REFRESH_SKEW_SECONDS,
    now: Optional[float] = None,
    trigger: str = "auto_refresh",
    emit_audit: bool = True,
) -> RefreshOutcome:
    """Refresh *record* via *refresh_fn*, re-encrypting the new tokens.

    Lifecycle (all in-memory; no DB):

    1. :func:`is_due` short-circuits with :data:`OUTCOME_NOT_DUE` if
       the row isn't within the skew window.  No audit row written
       (no event happened).
    2. ``refresh_token_enc is None`` (provider didn't issue one)
       short-circuits with :data:`OUTCOME_NO_REFRESH_TOKEN`.  Audit
       emits ``oauth.refresh`` outcome=``no_refresh_token`` so ops
       can track stuck rows.
    3. Decrypt the access + refresh ciphertext via
       :func:`token_vault.decrypt_for_user`.  Any vault error short-
       circuits with :data:`OUTCOME_VAULT_FAILURE`; audit emits
       ``oauth.refresh`` outcome=``provider_error`` (the audit
       vocabulary doesn't carry a "vault" outcome — operationally
       it's "couldn't refresh", which is what ``provider_error``
       means to the dashboard) with ``error`` prefixed
       ``vault:<class_name>`` for grep selectivity.
    4. ``await refresh_fn(refresh_plaintext)`` — caller-provided IdP
       roundtrip.  Any exception short-circuits with
       :data:`OUTCOME_PROVIDER_ERROR`; audit emits
       ``oauth.refresh`` outcome=``provider_error``.
    5. :func:`oauth_client.apply_rotation` merges the IdP response.
       :class:`oauth_client.TokenResponseError` short-circuits with
       :data:`OUTCOME_PROVIDER_ERROR`.
    6. :func:`token_vault.encrypt_for_user` re-wraps the new tokens.
       Builds ``new_record`` with ``version + 1``.
    7. Audit emits ``oauth.refresh`` outcome=``success``; if
       :func:`apply_rotation` reported ``rotated=True``, also emits
       ``oauth.token_rotated``.

    *trigger* must be in :data:`oauth_audit.ROTATION_TRIGGERS`
    (``{"auto_refresh", "explicit_refresh"}``).  ``auto_refresh`` is
    the default for the proactive-refresh path; an AS.2.5-style
    user-initiated "force refresh" passes ``"explicit_refresh"``.

    *emit_audit* is True by default; tests can pass False to skip
    the audit fan-out without monkey-patching the emitters.
    """

    if trigger not in oauth_audit.ROTATION_TRIGGERS:
        raise InvalidTriggerError(
            f"trigger {trigger!r} not in {sorted(oauth_audit.ROTATION_TRIGGERS)}"
        )

    if not is_due(record, skew_seconds=skew_seconds, now=now):
        return RefreshOutcome(
            outcome=OUTCOME_NOT_DUE,
            new_record=None,
            rotated=False,
            error=None,
            previous_expires_at=record.expires_at,
            new_expires_in_seconds=None,
            granted_scope=record.scope,
        )

    if record.refresh_token_enc is None:
        outcome = RefreshOutcome(
            outcome=OUTCOME_NO_REFRESH_TOKEN,
            new_record=None,
            rotated=False,
            error="no_refresh_token",
            previous_expires_at=record.expires_at,
            new_expires_in_seconds=None,
            granted_scope=record.scope,
        )
        if emit_audit:
            await _emit_refresh_audit(record, outcome)
        return outcome

    try:
        access_plaintext = token_vault.decrypt_for_user(
            record.user_id, record.provider, record.access_token_enc,
        )
        refresh_plaintext = token_vault.decrypt_for_user(
            record.user_id, record.provider, record.refresh_token_enc,
        )
    except TokenVaultError as exc:
        outcome = RefreshOutcome(
            outcome=OUTCOME_VAULT_FAILURE,
            new_record=None,
            rotated=False,
            error=f"vault:{type(exc).__name__}",
            previous_expires_at=record.expires_at,
            new_expires_in_seconds=None,
            granted_scope=record.scope,
        )
        if emit_audit:
            await _emit_refresh_audit(record, outcome)
        return outcome

    # Build a synthetic TokenSet to feed apply_rotation.  We only
    # need the four fields apply_rotation actually reads
    # (refresh_token + access_token + scope), so token_type / id_token
    # / raw default to neutral values.  This avoids re-computing
    # expires_at: parse_token_response (called inside apply_rotation)
    # uses the *fresh* payload's expires_in, not the previous record's.
    current = TokenSet(
        access_token=access_plaintext,
        refresh_token=refresh_plaintext,
        token_type="Bearer",
        expires_at=record.expires_at,
        scope=record.scope,
        id_token=None,
        raw={},
    )

    try:
        payload = await refresh_fn(refresh_plaintext)
    except Exception as exc:  # vendor adapter / network / 4xx-5xx
        outcome = RefreshOutcome(
            outcome=OUTCOME_PROVIDER_ERROR,
            new_record=None,
            rotated=False,
            error=f"refresh_fn:{type(exc).__name__}:{exc}"[:500],
            previous_expires_at=record.expires_at,
            new_expires_in_seconds=None,
            granted_scope=record.scope,
        )
        if emit_audit:
            await _emit_refresh_audit(record, outcome)
        return outcome

    try:
        new_token, rotated = oauth_client.apply_rotation(current, payload, now=now)
    except (TokenResponseError, TokenRefreshError) as exc:
        outcome = RefreshOutcome(
            outcome=OUTCOME_PROVIDER_ERROR,
            new_record=None,
            rotated=False,
            error=f"{type(exc).__name__}:{exc}"[:500],
            previous_expires_at=record.expires_at,
            new_expires_in_seconds=None,
            granted_scope=record.scope,
        )
        if emit_audit:
            await _emit_refresh_audit(record, outcome)
        return outcome

    new_access_enc = token_vault.encrypt_for_user(
        record.user_id, record.provider, new_token.access_token,
    )
    if new_token.refresh_token:
        new_refresh_enc: Optional[EncryptedToken] = token_vault.encrypt_for_user(
            record.user_id, record.provider, new_token.refresh_token,
        )
    else:
        # Provider didn't echo a refresh_token AND we had none — falls
        # through to None (apply_rotation only preserves the previous
        # one when *we* had one, which we did, so this branch is rare
        # but possible if the provider explicitly clears refresh).
        new_refresh_enc = None

    new_scope = new_token.scope or record.scope
    new_record = TokenVaultRecord(
        user_id=record.user_id,
        provider=record.provider,
        access_token_enc=new_access_enc,
        refresh_token_enc=new_refresh_enc,
        expires_at=new_token.expires_at,
        scope=new_scope,
        version=record.version + 1,
    )

    ts = time.time() if now is None else now
    if new_token.expires_at is not None:
        new_expires_in_seconds: Optional[int] = max(0, int(new_token.expires_at - ts))
    else:
        new_expires_in_seconds = None

    outcome = RefreshOutcome(
        outcome=OUTCOME_SUCCESS,
        new_record=new_record,
        rotated=rotated,
        error=None,
        previous_expires_at=record.expires_at,
        new_expires_in_seconds=new_expires_in_seconds,
        granted_scope=new_scope,
    )

    if emit_audit:
        await _emit_refresh_audit(record, outcome)
        if rotated and new_token.refresh_token:
            await oauth_audit.emit_token_rotated(
                oauth_audit.TokenRotatedContext(
                    provider=record.provider,
                    user_id=record.user_id,
                    previous_refresh_token=refresh_plaintext,
                    new_refresh_token=new_token.refresh_token,
                    triggered_by=trigger,
                )
            )
            # AS.6.5 — fan AS.5.1 ``auth.token_rotated`` rollup alongside
            # the AS.1.4 forensic ``oauth.token_rotated`` row above. Same
            # fingerprint contract — both refresh tokens are stored as
            # 12-char SHA-256 (PII redaction) by the AS.5.1 builder.
            from backend.security import auth_audit_bridge as _bridge
            await _bridge.emit_token_rotated_event(
                user_id=record.user_id,
                provider=record.provider,
                previous_refresh_token=refresh_plaintext,
                new_refresh_token=new_token.refresh_token,
                triggered_by=trigger,
            )

    return outcome


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internal helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _audit_outcome_for(outcome: RefreshOutcome) -> str:
    """Map an internal :class:`RefreshOutcome` value onto the
    :data:`oauth_audit.REFRESH_OUTCOMES` vocabulary.

    Audit only carries three outcomes (success / no_refresh_token /
    provider_error) per AS.1.4's contract.  The hook's extra
    ``vault_failure`` collapses onto ``provider_error`` (operationally
    "couldn't refresh"); ``not_due`` never reaches audit (no event
    happened) and is rejected explicitly here so a future refactor
    can't silently insert a misleading row.
    """
    if outcome.outcome == OUTCOME_SUCCESS:
        return oauth_audit.OUTCOME_SUCCESS
    if outcome.outcome == OUTCOME_NO_REFRESH_TOKEN:
        return oauth_audit.OUTCOME_NO_REFRESH_TOKEN
    if outcome.outcome in (OUTCOME_PROVIDER_ERROR, OUTCOME_VAULT_FAILURE):
        return oauth_audit.OUTCOME_PROVIDER_ERROR
    raise RefreshHookError(
        f"refusing to emit audit row for non-event outcome {outcome.outcome!r}"
    )


async def _emit_refresh_audit(
    record: TokenVaultRecord,
    outcome: RefreshOutcome,
) -> None:
    """Fan one ``oauth.refresh`` AS.1.4 forensic row + one
    ``auth.token_refresh`` AS.5.1 rollup row.

    The two rows coexist by AS.5.1 design: forensic captures every
    detail (granted_scope, previous_expires_at, raw error string)
    for the I8 chain verifier and the admin audit query surface;
    rollup captures the dashboard-visible outcome + new lifetime
    counter the AS.5.2 ``token_refresh_storm`` rule reads.

    Catches any audit-layer exception so a chain-append failure can't
    propagate past the hook (the hook's caller will already persist
    ``new_record`` regardless of audit success — losing a row of
    observability is not the same as losing a refresh).
    """
    audit_outcome = _audit_outcome_for(outcome)
    try:
        await oauth_audit.emit_refresh(
            oauth_audit.RefreshContext(
                provider=record.provider,
                user_id=record.user_id,
                outcome=audit_outcome,
                previous_expires_at=record.expires_at,
                new_expires_in_seconds=outcome.new_expires_in_seconds,
                granted_scope=outcome.granted_scope,
                error=outcome.error,
            )
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "oauth.refresh audit emit failed for %s/%s: %s",
            record.provider, record.user_id, exc,
        )

    # AS.6.5 — fan AS.5.1 ``auth.token_refresh`` rollup alongside the
    # forensic AS.1.4 row above. Bridge handles its own knob check
    # (AS.0.8 single-knob via ``auth_event.is_enabled``) and
    # exception swallow.  Rollup outcome vocabulary is byte-equal
    # the forensic vocabulary — both come from
    # :func:`_audit_outcome_for`.
    from backend.security import auth_audit_bridge as _bridge

    await _bridge.emit_token_refresh_event(
        user_id=record.user_id,
        provider=record.provider,
        outcome=audit_outcome,
        new_expires_in_seconds=outcome.new_expires_in_seconds,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


__all__ = [
    "ALL_OUTCOMES",
    "DEFAULT_REFRESH_SKEW_SECONDS",
    "InvalidTriggerError",
    "OUTCOME_NOT_DUE",
    "OUTCOME_NO_REFRESH_TOKEN",
    "OUTCOME_PROVIDER_ERROR",
    "OUTCOME_SUCCESS",
    "OUTCOME_VAULT_FAILURE",
    "RefreshCallable",
    "RefreshHookError",
    "RefreshOutcome",
    "TokenVaultRecord",
    "is_due",
    "is_enabled",
    "refresh_record",
]

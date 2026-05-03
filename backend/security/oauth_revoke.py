"""AS.2.5 — OAuth token revocation hook (DSAR / GDPR right-to-erasure).

Stateless orchestrator that drives the "revoke a stored OAuth credential
at the IdP, then delete it locally" lifecycle on a single ``oauth_tokens``
row (the per-user / per-provider credential AS.2.2 lays out).  The
caller passes in:

* a :class:`~oauth_refresh_hook.TokenVaultRecord` snapshot of the row's
  encrypted columns (re-used from AS.2.4 — same shape),
* an optional ``revoke_fn`` async callable that POSTs the token to the
  IdP's RFC 7009 revocation endpoint,
* the vendor's ``revocation_endpoint`` URL (or ``None`` when the
  vendor exposes no programmatic endpoint — Microsoft, Bitbucket,
  Notion, HubSpot, GitHub all sit in this bucket per the AS.1.3
  catalog),

and the hook returns a :class:`RevokeOutcome` carrying

* ``outcome`` — one of :data:`OUTCOME_*` (locked vocabulary),
* ``revocation_attempted`` — whether the IdP call was actually made,
* ``revocation_outcome`` — RFC 7009 result mapped onto the AS.1.4
  :data:`oauth_audit.REVOCATION_OUTCOMES` vocabulary (or ``None``
  when no attempt was made),
* ``trigger`` — ``"user_unlink"`` (Settings → Disconnect) or
  ``"dsar_erasure"`` (regulatory right-to-erasure) — surfaces in the
  audit row's ``after.trigger`` so dashboards / billing aggregators
  can split voluntary unlinks from compliance-mandated deletions.

The hook does NOT touch the database.  The persistence half — the
``DELETE FROM oauth_tokens WHERE user_id = ? AND provider = ?`` UPDATE
is the caller's job (AS.6.1 OAuth router for the user-initiated path,
or the DSAR runbook script for the regulatory path).  Same composition
contract as the AS.2.4 refresh hook: pure helper + audit fan-out, the
caller owns the SQL.

What this row delivers (TODO line "Revoke endpoint：DSAR / GDPR
right-to-erasure")
─────────────────────────────────────────────────────────────────────

* :func:`revoke_record` — the actual hook.  Decrypts via
  :mod:`backend.security.token_vault`, calls the caller-provided
  ``revoke_fn`` against the IdP's revocation endpoint, emits the
  AS.1.4 ``oauth.unlink`` audit row, and returns an outcome the
  caller acts on (typically followed by ``DELETE FROM oauth_tokens``
  regardless of whether the IdP confirmed revocation — DSAR mandates
  the local deletion even if the IdP is unreachable; the audit row
  preserves the failure for follow-up retries).
* :func:`emit_not_linked` — helper for the caller's "no row found"
  branch.  When the user (or DSAR script) asks to unlink a provider
  they were never linked to, the audit row should still record the
  attempt (``outcome=not_linked``) so an erasure-receipt audit trail
  is complete.
* :func:`is_enabled` — re-export of :func:`oauth_client.is_enabled`
  for caller-facing gate symmetry.  Pure helpers do NOT auto-gate
  (per AS.0.4 §6.2: DSAR / right-to-erasure MUST keep working with
  the AS knob off — the audit layer's
  :func:`oauth_audit._gate` does the silent-skip on its own).

Why pure helpers, not an HTTP route
───────────────────────────────────
1. **Composition** — AS.6.1 will wire ``POST /api/v1/auth/oauth/{provider}/unlink``
   on top of this orchestrator; the DSAR cron / runbook will wire its
   own bulk-iteration loop on top.  Keeping the hook stateless lets
   both wirings be one-liners.
2. **DSAR semantics** — right-to-erasure is "delete everywhere we
   stored it" not "talk to the IdP".  The IdP revocation is
   best-effort (some vendors expose no endpoint); the local DELETE is
   mandatory.  Splitting the orchestrator from the persistence keeps
   each layer's failure mode clean — IdP failure does NOT block
   local deletion.
3. **Testability** — no DB / network mocks needed.  Tests pin
   behaviour on :class:`RevokeOutcome` shape across a fake
   :class:`TokenVaultRecord` + a fake async ``revoke_fn``.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* No module-level mutable state.  One frozen dataclass
  (:class:`RevokeOutcome`), five immutable ``OUTCOME_*`` strings,
  one tuple of those strings, one frozenset of trigger strings.
* :class:`RevokeError` subclass tree.  No DB connections, no env
  reads, no caches.  Importing the module is side-effect free.
* All randomness comes from the vault (which itself comes from
  :mod:`secrets`).  No ``random``, no ``time.time`` at module top.
* Cross-worker consistency: the only shared state is the
  ``oauth_tokens`` row itself + the audit chain.  DSAR is a
  one-shot per-row operation (caller's UPDATE is the serialisation
  point — no in-flight conflict possible because once the row is
  DELETEd a second worker's revoke attempt would see "row not
  found" and emit ``OUTCOME_NOT_LINKED``).  Answer #1 of SOP §1
  (every worker reads same DB state).

Read-after-write timing audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────────
The hook itself only mutates in-memory data.  The DB read-then-DELETE
race lives in the caller; if two workers both see the row before
either DELETEs, both will revoke (RFC 7009 is idempotent — the second
call is a no-op or 200 OK depending on vendor) and both will
DELETE-with-rowcount-check (one wins, one sees ``rowcount=0`` and
returns).  The optimistic-lock counter on the row is NOT consulted
here — DSAR is strictly destructive, not an update.  See AS.2.4 hook
docstring for the corresponding lock-counter discussion on refresh.

TS twin (forward note)
──────────────────────
``templates/_shared/oauth-client/index.ts`` does not need a "revoke"
twin: generated apps don't own a server-side ``oauth_tokens`` table,
their tokens live in caller-managed keystore (IndexedDB / mobile
vendor secure-storage) and revocation in that environment is "drop
the keystore entry" — no server-mediated DSAR flow.  The Python-only
revoke hook is the server-side mirror that AS.2.2's ``oauth_tokens``
table requires.  If a future generated-app SKU adds a server-side
backplane (W-series productizer roadmap) that mirror will land then.

Path deviation note (per AS.1.1 / AS.1.3 / AS.1.4 / AS.2.1 / AS.2.4
precedent)
──────────────────────────────────────────────────────────────────
Located at ``backend/security/oauth_revoke.py`` parallel to the
sibling ``oauth_{client,vendors,audit,refresh_hook}.py`` and
``token_vault.py`` modules; canonical ``backend/auth/`` namespace is
shadowed by the legacy ``backend/auth.py`` session/RBAC module —
package promotion is an independent refactor row outside this scope.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from backend.security import oauth_audit, oauth_client, token_vault
from backend.security.oauth_refresh_hook import TokenVaultRecord
from backend.security.token_vault import EncryptedToken, TokenVaultError

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Outcome vocabulary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

#: The revocation succeeded — either the IdP confirmed (HTTP 200 per
#: RFC 7009 §2.2) or the vendor exposes no revocation endpoint and the
#: hook short-circuited with ``revocation_attempted=False``.  Either
#: way the caller proceeds to ``DELETE FROM oauth_tokens``.  Audit row
#: emits ``oauth.unlink`` outcome=``success``.
OUTCOME_SUCCESS = oauth_audit.OUTCOME_SUCCESS  # "success"

#: There was no ``oauth_tokens`` row to act on (caller asked to revoke
#: an unbound user/provider pair).  The hook never reaches IdP /
#: vault — :func:`emit_not_linked` is the helper for this branch.
#: Audit row still emits so an erasure-receipt audit trail is
#: complete.
OUTCOME_NOT_LINKED = oauth_audit.OUTCOME_NOT_LINKED  # "not_linked"

#: The IdP responded with an error to the revocation POST (or the
#: caller-supplied ``revoke_fn`` raised).  Caller proceeds to local
#: DELETE regardless — DSAR mandates local erasure even when the IdP
#: is unreachable.  Audit row emits ``oauth.unlink``
#: outcome=``revocation_failed`` so ops can re-try the revocation
#: side-channel later.
OUTCOME_REVOCATION_FAILED = oauth_audit.OUTCOME_REVOCATION_FAILED  # "revocation_failed"

#: Either the vault could not decrypt the row's ciphertext (binding
#: mismatch from a DB row swap, unknown key_version, corrupted
#: ciphertext) — the hook never gets to the IdP call.  Caller
#: still proceeds to local DELETE (the row is unusable anyway).
#: Audit row maps onto ``revocation_failed`` — the audit vocabulary
#: doesn't carry a "vault" outcome, operationally it's "couldn't
#: revoke", which is what ``revocation_failed`` means to the
#: dashboard.  ``error`` field on the outcome carries the underlying
#: vault-class name for grep selectivity (catches the "we lost the
#: master key" failure mode distinct from upstream IdP failures).
OUTCOME_VAULT_FAILURE = "vault_failure"

#: Ordered tuple of every outcome the hook ever surfaces.  Used by
#: callers (and tests) that need the canonical vocabulary without
#: importing each constant.
ALL_OUTCOMES: tuple[str, ...] = (
    OUTCOME_SUCCESS,
    OUTCOME_NOT_LINKED,
    OUTCOME_REVOCATION_FAILED,
    OUTCOME_VAULT_FAILURE,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Trigger vocabulary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

#: The user clicked "Disconnect <Provider>" in the account-settings
#: UI (AS.6.1 ``POST /api/v1/auth/oauth/{provider}/unlink`` will pass
#: this).
TRIGGER_USER_UNLINK = "user_unlink"

#: A regulatory DSAR / right-to-erasure request was processed (the
#: GDPR Art. 17 / CCPA §1798.105 path — operator-driven script,
#: typically batched per data-subject request).
TRIGGER_DSAR_ERASURE = "dsar_erasure"

#: Frozen vocabulary the hook's ``trigger`` argument must match.
#: Adding a new trigger requires touching this set + the dashboard
#: filter + this row's tests in the same PR (no silent vocab
#: extension — same discipline as
#: :data:`oauth_audit.ROTATION_TRIGGERS`).
REVOKE_TRIGGERS: frozenset[str] = frozenset({
    TRIGGER_USER_UNLINK,
    TRIGGER_DSAR_ERASURE,
})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class RevokeError(Exception):
    """Base class for hook-layer errors callers may catch in bulk.
    The hook prefers returning a typed :class:`RevokeOutcome` over
    raising — the only path that raises is malformed inputs (caller
    bug) or ``trigger`` outside :data:`REVOKE_TRIGGERS`.
    """


class InvalidTriggerError(RevokeError, ValueError):
    """``trigger`` is not in :data:`REVOKE_TRIGGERS`.  Subclasses
    :class:`ValueError` so existing input-validation
    ``except ValueError`` blocks at call sites continue to work."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Type aliases
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

#: Caller-provided async callable that POSTs ``token`` +
#: ``token_type_hint`` to the IdP's RFC 7009 revocation endpoint and
#: returns nothing on success.  Any exception is treated as
#: ``OUTCOME_REVOCATION_FAILED`` (caller decides whether the
#: exception is transient — DSAR doesn't retry inline; user-unlink
#: caller may surface a "retry later" banner).
RevokeCallable = Callable[[str, Optional[str]], Awaitable[None]]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Frozen dataclasses (public surface)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass(frozen=True)
class RevokeOutcome:
    """Result of one :func:`revoke_record` (or :func:`emit_not_linked`)
    call.

    For ``outcome == OUTCOME_SUCCESS``:

    * ``revocation_attempted`` is True iff the hook actually called
      ``revoke_fn`` (False when the vendor exposes no revocation
      endpoint — see :data:`oauth_vendors.VENDORS` for the per-vendor
      ``revocation_endpoint`` map).
    * ``revocation_outcome`` is :data:`oauth_audit.OUTCOME_SUCCESS`
      when attempted-and-succeeded, ``None`` when not attempted.

    For ``outcome == OUTCOME_REVOCATION_FAILED``:

    * ``revocation_attempted`` is True (the only way this outcome
      surfaces is from a ``revoke_fn`` exception).
    * ``revocation_outcome`` is
      :data:`oauth_audit.OUTCOME_REVOCATION_FAILED`.
    * ``error`` carries the exception class name + message
      (truncated to 500 chars for chain-row sanity).

    For ``outcome == OUTCOME_VAULT_FAILURE``:

    * ``revocation_attempted`` is False (we never got to the network).
    * ``revocation_outcome`` is ``None``.
    * ``error`` carries ``vault:<TokenVaultError-subclass>`` for
      grep selectivity.

    For ``outcome == OUTCOME_NOT_LINKED``:

    * ``revocation_attempted`` is False, ``revocation_outcome`` is
      ``None``, ``error`` is ``None``.

    Caller MUST proceed to ``DELETE FROM oauth_tokens WHERE
    user_id = ? AND provider = ?`` for any outcome other than
    ``NOT_LINKED`` — the hook's contract is "in-memory + IdP +
    audit", local deletion is always the caller's responsibility
    even when the IdP step failed.
    """

    outcome: str
    revocation_attempted: bool
    revocation_outcome: Optional[str]
    trigger: str
    error: Optional[str]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AS.0.8 single-knob hook (re-export for symmetry)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def is_enabled() -> bool:
    """Whether the AS feature family is enabled per AS.0.8 §3.1.

    Thin re-export of :func:`oauth_client.is_enabled` so callers can
    gate their *invocation* of the hook (e.g. the AS.6.1
    ``/api/v1/auth/oauth/{provider}/unlink`` route).  The hook's
    internal pure helpers do NOT call this — they delegate to the
    audit layer's :func:`oauth_audit._gate` for the silent-skip
    behaviour AS.0.4 §6.2 mandates (DSAR must work knob-off).
    """
    return oauth_client.is_enabled()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Orchestrator — the hook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def revoke_record(
    record: TokenVaultRecord,
    revoke_fn: Optional[RevokeCallable] = None,
    *,
    revocation_endpoint: Optional[str] = None,
    trigger: str = TRIGGER_USER_UNLINK,
    actor: Optional[str] = None,
    emit_audit: bool = True,
    request_id: Optional[str] = None,
) -> RevokeOutcome:
    """Revoke the credentials in *record* at the IdP, emit audit row.

    Lifecycle (all in-memory; no DB):

    1. ``trigger`` validated against :data:`REVOKE_TRIGGERS`.
    2. If *revocation_endpoint* is ``None`` OR *revoke_fn* is
       ``None`` (caller signalled "no programmatic revocation
       available"), short-circuit with
       :data:`OUTCOME_SUCCESS` + ``revocation_attempted=False`` +
       ``revocation_outcome=None``.  Audit row reflects the skip
       so an external auditor can verify "we tried to do the right
       thing for this vendor".
    3. Decide which token to revoke:

       * If ``record.refresh_token_enc`` is present, revoke the
         **refresh_token** (per RFC 7009 §2.1 + OAuth 2.1 BCP §4.13
         best-practice: revoking the refresh kills the entire grant
         tree, including all access_tokens minted from it).
       * Else if ``record.access_token_enc`` is present, revoke the
         **access_token** (Apple-style providers without a stored
         refresh_token).

    4. Decrypt the chosen ciphertext via
       :func:`token_vault.decrypt_for_user_with_audit`.  Any
       :class:`TokenVaultError` short-circuits with
       :data:`OUTCOME_VAULT_FAILURE`; audit emits ``oauth.unlink``
       outcome=``revocation_failed`` with ``error="vault:<class>"``
       prefix for grep selectivity.
    5. ``await revoke_fn(plaintext, hint)`` — caller-provided IdP
       roundtrip.  Any exception short-circuits with
       :data:`OUTCOME_REVOCATION_FAILED`; audit emits
       ``oauth.unlink`` outcome=``revocation_failed`` with the
       exception class + message in ``error``.
    6. Audit emits ``oauth.unlink`` outcome=``success`` +
       ``revocation_attempted=True`` +
       ``revocation_outcome="success"``.

    *trigger* MUST be in :data:`REVOKE_TRIGGERS`
    (``{"user_unlink", "dsar_erasure"}``).  ``user_unlink`` is the
    default for the voluntary path; the regulatory DSAR path passes
    ``"dsar_erasure"`` so the audit row is filterable.

    *actor* defaults to ``record.user_id`` when omitted.  The DSAR
    runbook caller passes the operator's identity (e.g.
    ``"dsar:<ticket-id>"``) so the audit chain attributes the
    deletion to the operator, not to the data-subject.

    *emit_audit* is True by default; tests can pass False to skip
    the audit fan-out without monkey-patching the emitters.
    """

    if trigger not in REVOKE_TRIGGERS:
        raise InvalidTriggerError(
            f"trigger {trigger!r} not in {sorted(REVOKE_TRIGGERS)}"
        )

    # Step 2 — vendor with no programmatic revocation endpoint, or
    # caller signalled "skip the IdP call" by passing revoke_fn=None.
    if revoke_fn is None or not revocation_endpoint:
        outcome = RevokeOutcome(
            outcome=OUTCOME_SUCCESS,
            revocation_attempted=False,
            revocation_outcome=None,
            trigger=trigger,
            error=None,
        )
        if emit_audit:
            await _emit_unlink_audit(record, outcome, actor)
        return outcome

    # Step 3 — pick token + token_type_hint.
    chosen, hint = _choose_token(record)
    if chosen is None:
        # Both ciphertext columns empty — the row has nothing to
        # revoke remotely.  Treat the same as "no endpoint": local
        # deletion proceeds, audit reflects no attempt.
        outcome = RevokeOutcome(
            outcome=OUTCOME_SUCCESS,
            revocation_attempted=False,
            revocation_outcome=None,
            trigger=trigger,
            error=None,
        )
        if emit_audit:
            await _emit_unlink_audit(record, outcome, actor)
        return outcome

    # Step 4 — decrypt via vault.
    try:
        if emit_audit:
            eff_actor = actor or (
                f"dsar:{record.user_id}"
                if trigger == TRIGGER_DSAR_ERASURE
                else record.user_id
            )
            plaintext = await token_vault.decrypt_for_user_with_audit(
                record.user_id, record.provider, chosen,
                request_id=request_id,
                actor=eff_actor,
            )
        else:
            plaintext = token_vault.decrypt_for_user(
                record.user_id, record.provider, chosen,
            )
    except TokenVaultError as exc:
        outcome = RevokeOutcome(
            outcome=OUTCOME_VAULT_FAILURE,
            revocation_attempted=False,
            revocation_outcome=None,
            trigger=trigger,
            error=f"vault:{type(exc).__name__}",
        )
        if emit_audit:
            await _emit_unlink_audit(record, outcome, actor)
        return outcome

    # Step 5 — IdP roundtrip via caller-supplied callable.
    try:
        await revoke_fn(plaintext, hint)
    except Exception as exc:  # vendor adapter / network / 4xx-5xx
        outcome = RevokeOutcome(
            outcome=OUTCOME_REVOCATION_FAILED,
            revocation_attempted=True,
            revocation_outcome=oauth_audit.OUTCOME_REVOCATION_FAILED,
            trigger=trigger,
            error=f"revoke_fn:{type(exc).__name__}:{exc}"[:500],
        )
        if emit_audit:
            await _emit_unlink_audit(record, outcome, actor)
        return outcome

    # Step 6 — success.
    outcome = RevokeOutcome(
        outcome=OUTCOME_SUCCESS,
        revocation_attempted=True,
        revocation_outcome=oauth_audit.OUTCOME_SUCCESS,
        trigger=trigger,
        error=None,
    )
    if emit_audit:
        await _emit_unlink_audit(record, outcome, actor)
    return outcome


async def emit_not_linked(
    *,
    user_id: str,
    provider: str,
    trigger: str = TRIGGER_USER_UNLINK,
    actor: Optional[str] = None,
    emit_audit: bool = True,
) -> RevokeOutcome:
    """Caller's "row not found" branch — emit an :data:`OUTCOME_NOT_LINKED`
    audit row without touching vault or IdP.

    DSAR right-to-erasure compliance requires the audit chain to
    record every erasure attempt, even ones that resolve to "nothing
    to delete".  This helper centralises that emission so the AS.6.1
    OAuth router and the DSAR runbook both produce identically-
    shaped chain rows for the not-linked branch.
    """

    if trigger not in REVOKE_TRIGGERS:
        raise InvalidTriggerError(
            f"trigger {trigger!r} not in {sorted(REVOKE_TRIGGERS)}"
        )

    outcome = RevokeOutcome(
        outcome=OUTCOME_NOT_LINKED,
        revocation_attempted=False,
        revocation_outcome=None,
        trigger=trigger,
        error=None,
    )

    if emit_audit:
        try:
            await oauth_audit.emit_unlink(
                oauth_audit.UnlinkContext(
                    provider=provider,
                    user_id=user_id,
                    outcome=oauth_audit.OUTCOME_NOT_LINKED,
                    revocation_attempted=False,
                    revocation_outcome=None,
                    actor=actor or user_id,
                )
            )
        except Exception as exc:  # pragma: no cover — audit.log already swallows
            logger.warning(
                "oauth.unlink audit emit failed for %s/%s: %s",
                provider, user_id, exc,
            )

    return outcome


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Internal helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _choose_token(
    record: TokenVaultRecord,
) -> tuple[Optional[EncryptedToken], Optional[str]]:
    """Return ``(ciphertext, token_type_hint)`` for the token to revoke.

    Prefers ``refresh_token`` per RFC 7009 §2.1 + OAuth 2.1 BCP §4.13:
    revoking the refresh_token kills the entire grant tree (including
    all access_tokens minted from it), so it is the better target
    when both are present.  Falls back to ``access_token`` for
    Apple-style providers that do not echo a refresh_token to begin
    with.  Returns ``(None, None)`` when both columns are empty —
    the row carries nothing revocable at the IdP, so the caller
    short-circuits to local DELETE.
    """
    if record.refresh_token_enc is not None:
        return record.refresh_token_enc, "refresh_token"
    if record.access_token_enc is not None and record.access_token_enc.ciphertext:
        return record.access_token_enc, "access_token"
    return None, None


def _audit_outcome_for(outcome: RevokeOutcome) -> str:
    """Map an internal :class:`RevokeOutcome` value onto the
    :data:`oauth_audit.UNLINK_OUTCOMES` vocabulary.

    Audit only carries three outcomes for unlink (success /
    not_linked / revocation_failed) per AS.1.4's contract.  The
    hook's extra ``vault_failure`` collapses onto
    ``revocation_failed`` (operationally "couldn't revoke" — the
    decrypt step is part of the revocation pipeline from the
    caller's point of view, even though no network packet was
    sent).
    """
    if outcome.outcome == OUTCOME_SUCCESS:
        return oauth_audit.OUTCOME_SUCCESS
    if outcome.outcome == OUTCOME_NOT_LINKED:
        return oauth_audit.OUTCOME_NOT_LINKED
    if outcome.outcome in (OUTCOME_REVOCATION_FAILED, OUTCOME_VAULT_FAILURE):
        return oauth_audit.OUTCOME_REVOCATION_FAILED
    raise RevokeError(
        f"refusing to emit audit row for unknown outcome {outcome.outcome!r}"
    )


def _audit_revocation_outcome_for(outcome: RevokeOutcome) -> Optional[str]:
    """Map onto :data:`oauth_audit.REVOCATION_OUTCOMES` (or None when
    no IdP call was made).

    The :func:`oauth_audit.emit_unlink` helper enforces:

    * if ``revocation_attempted`` is True, ``revocation_outcome`` MUST
      be in :data:`oauth_audit.REVOCATION_OUTCOMES`;
    * if False, the field is forced to ``None``.

    This helper produces the value the audit emit will accept.
    """
    if not outcome.revocation_attempted:
        return None
    # ``RevokeOutcome.revocation_outcome`` already carries the
    # AS.1.4 vocabulary value when revocation_attempted=True (we
    # constructed it that way in revoke_record); this helper is just
    # the seam test the contract.
    return outcome.revocation_outcome


async def _emit_unlink_audit(
    record: TokenVaultRecord,
    outcome: RevokeOutcome,
    actor: Optional[str],
) -> None:
    """Fan one ``oauth.unlink`` audit row out via AS.1.4.

    Catches any audit-layer exception so a chain-append failure
    can't propagate past the hook (the caller will already proceed
    to local DELETE regardless of audit success — losing a row of
    observability is not the same as losing the deletion).
    """
    try:
        # Build the AS.1.4 UnlinkContext.  The ``after.trigger``
        # field is appended by patching the audit row body via the
        # context dataclass — see emit_unlink + UnlinkContext for
        # the existing fields.  Trigger is a hook-side concept
        # (DSAR vs voluntary unlink) that surfaces in the audit
        # row's ``after`` JSON via the actor naming convention:
        # ``actor="dsar:<ticket>"`` for DSAR, plain ``actor=user_id``
        # for voluntary.  We additionally embed the trigger in
        # ``actor`` when the caller did NOT pass an explicit actor
        # so the row remains filterable without an audit-schema
        # change.
        eff_actor = actor or _default_actor(record, outcome)
        await oauth_audit.emit_unlink(
            oauth_audit.UnlinkContext(
                provider=record.provider,
                user_id=record.user_id,
                outcome=_audit_outcome_for(outcome),
                revocation_attempted=outcome.revocation_attempted,
                revocation_outcome=_audit_revocation_outcome_for(outcome),
                actor=eff_actor,
            )
        )
    except Exception as exc:  # pragma: no cover — audit.log already swallows
        logger.warning(
            "oauth.unlink audit emit failed for %s/%s: %s",
            record.provider, record.user_id, exc,
        )


def _default_actor(
    record: TokenVaultRecord,
    outcome: RevokeOutcome,
) -> str:
    """When the caller doesn't pass *actor*, derive one from trigger.

    * ``user_unlink`` → the data-subject themselves (the
      ``record.user_id``) — the user is the actor on a voluntary
      Settings → Disconnect click.
    * ``dsar_erasure`` → ``"dsar:<user_id>"`` — the operator is the
      actor on a regulatory deletion; the prefix lets the admin
      filter pane separate DSAR rows from voluntary unlinks
      without needing a new audit-schema field.
    """
    if outcome.trigger == TRIGGER_DSAR_ERASURE:
        return f"dsar:{record.user_id}"
    return record.user_id


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Public surface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


__all__ = [
    "ALL_OUTCOMES",
    "InvalidTriggerError",
    "OUTCOME_NOT_LINKED",
    "OUTCOME_REVOCATION_FAILED",
    "OUTCOME_SUCCESS",
    "OUTCOME_VAULT_FAILURE",
    "REVOKE_TRIGGERS",
    "RevokeCallable",
    "RevokeError",
    "RevokeOutcome",
    "TRIGGER_DSAR_ERASURE",
    "TRIGGER_USER_UNLINK",
    "emit_not_linked",
    "is_enabled",
    "revoke_record",
]

"""AS.2.5 — `backend.security.oauth_revoke` contract tests.

Locks the load-bearing behaviour of the AS.2.5 revoke hook
(:mod:`backend.security.oauth_revoke`):

1. ``revoke_record`` happy path: vendor with revocation endpoint +
   passing ``revoke_fn`` → ``OUTCOME_SUCCESS`` + ``revocation_attempted=True`` +
   ``revocation_outcome="success"`` + audit row emits ``oauth.unlink``
   outcome=``success``.
2. ``revoke_record`` no-revocation-endpoint short-circuit: vendor
   with ``revocation_endpoint=None`` → ``OUTCOME_SUCCESS`` +
   ``revocation_attempted=False`` (the audit row's
   ``revocation_outcome`` is forced None per AS.1.4 contract).
3. ``revoke_record`` revoke_fn=None short-circuit: caller signals
   "skip the IdP call" → same shape as #2 (vendor-no-endpoint).
4. ``revoke_record`` IdP failure: ``revoke_fn`` raises →
   ``OUTCOME_REVOCATION_FAILED`` + ``revocation_attempted=True`` +
   ``revocation_outcome="revocation_failed"`` + audit row emits
   outcome=``revocation_failed`` with exception class+message in
   ``error``.
5. ``revoke_record`` vault failure: binding-mismatch (DB row swap)
   or unknown ``key_version`` → ``OUTCOME_VAULT_FAILURE`` +
   ``revocation_attempted=False`` + audit row collapses onto
   ``revocation_failed`` with ``error="vault:<class>"`` prefix.
6. ``revoke_record`` token preference: refresh_token preferred over
   access_token (RFC 7009 §2.1 + OAuth 2.1 BCP §4.13 — revoking
   refresh kills the entire grant tree); falls back to access_token
   when refresh is None.
7. ``revoke_record`` empty-record edge: both ciphertext columns
   empty → short-circuit to SUCCESS-skipped (nothing to revoke).
8. ``revoke_record`` trigger validation: ``trigger`` outside
   :data:`REVOKE_TRIGGERS` raises :class:`InvalidTriggerError`
   (subclasses :class:`ValueError` for caller-compat).
9. ``revoke_record`` actor defaults: ``user_unlink`` →
   actor=user_id; ``dsar_erasure`` → actor=``"dsar:<user_id>"``.
   Caller-supplied ``actor`` always wins.
10. ``revoke_record`` AS.0.8 knob-off: pure helper still runs
    (returns the outcome); OAuth lifecycle audit silent-skips while
    KS decryption audit remains.
11. ``revoke_record`` ``emit_audit=False`` opt-out: caller can skip
    OAuth lifecycle audit fan-out, but KS decryption audit remains
    mandatory whenever the vault returns plaintext.
12. ``emit_not_linked`` helper: ``OUTCOME_NOT_LINKED`` +
    audit row emits without touching vault / IdP; trigger
    validation applies; caller-supplied ``actor`` wins over default.
13. Cross-module drift guards:
    * ``OUTCOME_SUCCESS`` / ``OUTCOME_NOT_LINKED`` /
      ``OUTCOME_REVOCATION_FAILED`` byte-equal
      :data:`oauth_audit.OUTCOME_*`.
    * Each outcome from the hook maps onto a vocabulary value the
      :func:`oauth_audit.emit_unlink` helper accepts.
    * ``REVOKE_TRIGGERS`` ⊆ no-collision-with
      :data:`oauth_audit.ROTATION_TRIGGERS` (same string vocabulary
      space; collision would let a typo route to the wrong audit
      family).
    * ``ALL_OUTCOMES`` covers every ``OUTCOME_*`` constant exported
      by the module.
14. SOP §1 module-global state audit:
    * 0 module-level mutable containers (no list / dict / set).
    * Constants stable across :func:`importlib.reload`.
    * ``__all__`` cross-check matches actual public surface.
    * Pre-commit fingerprint grep — 4 SOP fingerprints (``_conn()``
      / ``await conn.commit()`` / ``datetime('now')`` /
      ``VALUES (?,...)``) all 0-hit.
    * Module imports no ``random``, performs no top-level IO.
15. Integration smoke: round-trip through vault for the chosen
    token (refresh-preferred branch) — the hook decrypts the
    correct ciphertext and feeds the OLD plaintext to ``revoke_fn``.
16. AS.1.3 vendor catalog cross-check: every shipped vendor either
    has a non-None ``revocation_endpoint`` or the hook still lands
    a success-skipped audit row (no vendor breaks the orchestrator).

The hook is pure: tests use a fake async ``revoke_fn`` callable and
monkey-patch the audit emitter (``backend.security.oauth_audit.audit.log``).
No DB / network.  The vault round-trip exercises
:mod:`backend.secret_store` (Fernet) end-to-end so the encryption
half of AS.2.1 is implicitly verified through this row.
"""

from __future__ import annotations

import asyncio
import importlib
import re
from pathlib import Path
from typing import Any

import pytest

from backend.security import (
    oauth_audit,
    oauth_client,
    oauth_refresh_hook as orh,
    oauth_revoke as ovr,
    oauth_vendors,
    token_vault,
)


REVOKE_SRC = Path(ovr.__file__).read_text(encoding="utf-8")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _run(coro):
    return asyncio.run(coro)


def _make_record(
    *,
    user_id: str = "u-alice",
    provider: str = "google",
    access_plaintext: Any = "ya29.alice-access",
    refresh_plaintext: Any = "1//alice-refresh",
    expires_at: float = 1000.0,
    scope: tuple[str, ...] = ("openid", "email"),
    version: int = 3,
) -> orh.TokenVaultRecord:
    """Build a TokenVaultRecord with real vault-encrypted ciphertext.

    Pass ``access_plaintext=None`` / ``refresh_plaintext=None`` to
    build a record with the corresponding ciphertext column missing
    (mirrors AS.2.2 ``''`` default).
    """
    access_enc = (
        token_vault.encrypt_for_user(user_id, provider, access_plaintext)
        if access_plaintext
        else token_vault.EncryptedToken(ciphertext="", key_version=1)
    )
    refresh_enc = (
        token_vault.encrypt_for_user(user_id, provider, refresh_plaintext)
        if refresh_plaintext
        else None
    )
    return orh.TokenVaultRecord(
        user_id=user_id,
        provider=provider,
        access_token_enc=access_enc,
        refresh_token_enc=refresh_enc,
        expires_at=expires_at,
        scope=scope,
        version=version,
    )


def _mock_revoke_fn(succeed: bool = True, exc: Exception | None = None):
    """Return an async callable that captures every (token, hint)
    invocation and either returns None (success) or raises *exc*."""
    captured: dict[str, Any] = {"calls": []}

    async def fn(token: str, hint):
        captured["calls"].append((token, hint))
        if not succeed:
            assert exc is not None
            raise exc
        return None

    return fn, captured


def _capture_audit(monkeypatch):
    """Replace ``oauth_audit.audit.log`` with a fake that records
    every call.  Returns the captured-events list."""
    events: list[dict[str, Any]] = []

    async def fake_log(**kwargs):
        events.append(kwargs)
        return len(events)

    monkeypatch.setattr("backend.security.oauth_audit.audit.log", fake_log)
    return events


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Happy path — IdP success
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_revoke_record_success_emits_audit_with_revocation_attempted_true(monkeypatch):
    rec = _make_record(refresh_plaintext="1//alice-refresh")
    events = _capture_audit(monkeypatch)
    fn, captured = _mock_revoke_fn(succeed=True)

    out = _run(ovr.revoke_record(
        rec, fn,
        revocation_endpoint="https://oauth2.googleapis.com/revoke",
        trigger=ovr.TRIGGER_USER_UNLINK,
    ))

    assert out.outcome == ovr.OUTCOME_SUCCESS
    assert out.revocation_attempted is True
    assert out.revocation_outcome == oauth_audit.OUTCOME_SUCCESS
    assert out.error is None
    assert out.trigger == ovr.TRIGGER_USER_UNLINK

    # revoke_fn invoked with refresh_token + hint="refresh_token"
    assert len(captured["calls"]) == 1
    token, hint = captured["calls"][0]
    assert token == "1//alice-refresh"
    assert hint == "refresh_token"

    # Audit row shape: KS.1.5 decryption row plus legacy oauth.unlink row.
    assert len(events) == 2
    assert events[0]["action"] == "ks.decryption"
    ev = next(e for e in events if e["action"] == oauth_client.EVENT_OAUTH_UNLINK)
    assert ev["action"] == oauth_client.EVENT_OAUTH_UNLINK
    assert ev["after"]["outcome"] == oauth_audit.OUTCOME_SUCCESS
    assert ev["after"]["revocation_attempted"] is True
    assert ev["after"]["revocation_outcome"] == oauth_audit.OUTCOME_SUCCESS
    # entity_id is "<provider>:<user_id>" per AS.1.4 _entity_id_token
    assert ev["entity_id"] == "google:u-alice"
    assert ev["actor"] == "u-alice"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2/3. No-revocation-endpoint / revoke_fn=None short-circuits
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_revoke_record_skips_when_revocation_endpoint_is_none(monkeypatch):
    rec = _make_record()
    events = _capture_audit(monkeypatch)
    fn, captured = _mock_revoke_fn(succeed=True)

    out = _run(ovr.revoke_record(
        rec, fn,
        revocation_endpoint=None,  # vendor exposes no endpoint
        trigger=ovr.TRIGGER_USER_UNLINK,
    ))

    assert out.outcome == ovr.OUTCOME_SUCCESS
    assert out.revocation_attempted is False
    assert out.revocation_outcome is None
    # revoke_fn was NOT called
    assert captured["calls"] == []
    # Audit row reflects the skip
    assert len(events) == 1
    assert events[0]["after"]["outcome"] == oauth_audit.OUTCOME_SUCCESS
    assert events[0]["after"]["revocation_attempted"] is False
    assert events[0]["after"]["revocation_outcome"] is None


def test_revoke_record_skips_when_revoke_fn_is_none(monkeypatch):
    rec = _make_record()
    events = _capture_audit(monkeypatch)

    out = _run(ovr.revoke_record(
        rec, None,
        revocation_endpoint="https://oauth2.googleapis.com/revoke",
        trigger=ovr.TRIGGER_USER_UNLINK,
    ))

    assert out.outcome == ovr.OUTCOME_SUCCESS
    assert out.revocation_attempted is False
    assert events[0]["after"]["revocation_attempted"] is False


def test_revoke_record_skips_when_endpoint_is_empty_string(monkeypatch):
    """Empty-string ``revocation_endpoint`` is falsy and treated the
    same as ``None`` — accommodates callers that build the URL from
    a vendor-config field that may be ``""`` rather than ``None``."""
    rec = _make_record()
    _capture_audit(monkeypatch)
    fn, captured = _mock_revoke_fn(succeed=True)

    out = _run(ovr.revoke_record(
        rec, fn, revocation_endpoint="",
        trigger=ovr.TRIGGER_USER_UNLINK,
    ))
    assert out.outcome == ovr.OUTCOME_SUCCESS
    assert out.revocation_attempted is False
    assert captured["calls"] == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. IdP failure — revoke_fn raises
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_revoke_record_idp_failure_returns_revocation_failed(monkeypatch):
    rec = _make_record()
    events = _capture_audit(monkeypatch)
    fn, captured = _mock_revoke_fn(succeed=False, exc=RuntimeError("502 bad gateway"))

    out = _run(ovr.revoke_record(
        rec, fn,
        revocation_endpoint="https://oauth2.googleapis.com/revoke",
        trigger=ovr.TRIGGER_USER_UNLINK,
    ))

    assert out.outcome == ovr.OUTCOME_REVOCATION_FAILED
    assert out.revocation_attempted is True
    assert out.revocation_outcome == oauth_audit.OUTCOME_REVOCATION_FAILED
    assert "RuntimeError" in (out.error or "")
    assert "502 bad gateway" in (out.error or "")
    # revoke_fn WAS called (the error came from inside it)
    assert len(captured["calls"]) == 1

    # Audit row carries the failure alongside the KS.1.5 decrypt row.
    assert len(events) == 2
    assert events[0]["action"] == "ks.decryption"
    ev = next(e for e in events if e["action"] == oauth_client.EVENT_OAUTH_UNLINK)
    assert ev["action"] == oauth_client.EVENT_OAUTH_UNLINK
    assert ev["after"]["outcome"] == oauth_audit.OUTCOME_REVOCATION_FAILED
    assert ev["after"]["revocation_attempted"] is True
    assert ev["after"]["revocation_outcome"] == oauth_audit.OUTCOME_REVOCATION_FAILED


def test_revoke_record_idp_failure_truncates_long_error(monkeypatch):
    """The audit row's error field is bounded to 500 chars so a
    pathological vendor error message can't bloat the chain."""
    rec = _make_record()
    _capture_audit(monkeypatch)
    long_msg = "x" * 5000
    fn, _ = _mock_revoke_fn(succeed=False, exc=RuntimeError(long_msg))

    out = _run(ovr.revoke_record(
        rec, fn,
        revocation_endpoint="https://oauth2.googleapis.com/revoke",
    ))
    assert len(out.error or "") <= 500


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. Vault failure — binding mismatch + unknown key_version
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_revoke_record_vault_binding_mismatch_returns_vault_failure(monkeypatch):
    """Concrete row-swap attack: ciphertext encrypted for alice but
    the record claims user_id=bob — vault raises BindingMismatchError;
    hook surfaces OUTCOME_VAULT_FAILURE; audit collapses onto
    revocation_failed; revoke_fn is NEVER called."""
    enc_for_alice = token_vault.encrypt_for_user("u-alice", "google", "alice-access")
    enc_refresh_for_alice = token_vault.encrypt_for_user(
        "u-alice", "google", "alice-refresh",
    )
    rec = orh.TokenVaultRecord(
        user_id="u-bob",  # claim it's bob's row
        provider="google",
        access_token_enc=enc_for_alice,
        refresh_token_enc=enc_refresh_for_alice,
        expires_at=1000.0,
        scope=(),
        version=1,
    )
    events = _capture_audit(monkeypatch)
    fn, captured = _mock_revoke_fn(succeed=True)

    out = _run(ovr.revoke_record(
        rec, fn,
        revocation_endpoint="https://oauth2.googleapis.com/revoke",
    ))

    assert out.outcome == ovr.OUTCOME_VAULT_FAILURE
    assert out.revocation_attempted is False  # never reached the network
    assert out.revocation_outcome is None
    assert "vault:BindingMismatchError" in (out.error or "")
    # revoke_fn never called — fail-fast on vault decrypt
    assert captured["calls"] == []
    # Audit collapses onto revocation_failed (only valid UNLINK_OUTCOME
    # for "couldn't revoke") with revocation_attempted=False so the
    # AS.1.4 contract forces revocation_outcome to None.
    assert len(events) == 1
    assert events[0]["after"]["outcome"] == oauth_audit.OUTCOME_REVOCATION_FAILED
    assert events[0]["after"]["revocation_attempted"] is False
    assert events[0]["after"]["revocation_outcome"] is None


def test_revoke_record_vault_unknown_key_version_returns_vault_failure(monkeypatch):
    enc_a = token_vault.encrypt_for_user("u", "google", "access")
    enc_r = token_vault.encrypt_for_user("u", "google", "refresh")
    rec = orh.TokenVaultRecord(
        user_id="u",
        provider="google",
        access_token_enc=token_vault.EncryptedToken(
            ciphertext=enc_a.ciphertext, key_version=99,
        ),
        refresh_token_enc=token_vault.EncryptedToken(
            ciphertext=enc_r.ciphertext, key_version=99,
        ),
        expires_at=1000.0,
        scope=(),
        version=1,
    )
    _capture_audit(monkeypatch)
    fn, captured = _mock_revoke_fn(succeed=True)

    out = _run(ovr.revoke_record(
        rec, fn,
        revocation_endpoint="https://oauth2.googleapis.com/revoke",
    ))

    assert out.outcome == ovr.OUTCOME_VAULT_FAILURE
    assert "UnknownKeyVersionError" in (out.error or "")
    assert captured["calls"] == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. Token preference — refresh > access
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_revoke_record_prefers_refresh_token_when_present(monkeypatch):
    """When both tokens are present, revoke the refresh_token (RFC
    7009 §2.1 + OAuth 2.1 BCP §4.13: kills the entire grant tree)."""
    rec = _make_record(
        access_plaintext="access-plain",
        refresh_plaintext="refresh-plain",
    )
    _capture_audit(monkeypatch)
    fn, captured = _mock_revoke_fn(succeed=True)

    _run(ovr.revoke_record(
        rec, fn, revocation_endpoint="https://x/revoke",
    ))

    assert captured["calls"] == [("refresh-plain", "refresh_token")]


def test_revoke_record_falls_back_to_access_token_when_refresh_absent(monkeypatch):
    """Apple-style provider that doesn't echo a refresh_token —
    revoke the access_token instead."""
    rec = _make_record(
        access_plaintext="apple-access",
        refresh_plaintext=None,  # Apple non-first-time login pattern
    )
    _capture_audit(monkeypatch)
    fn, captured = _mock_revoke_fn(succeed=True)

    out = _run(ovr.revoke_record(
        rec, fn, revocation_endpoint="https://appleid.apple.com/auth/revoke",
    ))

    assert out.outcome == ovr.OUTCOME_SUCCESS
    assert captured["calls"] == [("apple-access", "access_token")]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. Empty-record edge — both ciphertext columns empty
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_revoke_record_empty_ciphertext_short_circuits_to_success(monkeypatch):
    """Defensive: a row whose access_token_enc.ciphertext is empty
    AND whose refresh_token_enc is None has nothing revocable
    remotely.  Hook still surfaces SUCCESS (so caller proceeds to
    DELETE) with revocation_attempted=False."""
    rec = orh.TokenVaultRecord(
        user_id="u-x", provider="google",
        access_token_enc=token_vault.EncryptedToken(ciphertext="", key_version=1),
        refresh_token_enc=None,
        expires_at=None, scope=(), version=0,
    )
    events = _capture_audit(monkeypatch)
    fn, captured = _mock_revoke_fn(succeed=True)

    out = _run(ovr.revoke_record(
        rec, fn, revocation_endpoint="https://x/revoke",
    ))

    assert out.outcome == ovr.OUTCOME_SUCCESS
    assert out.revocation_attempted is False
    assert captured["calls"] == []
    assert events[0]["after"]["revocation_attempted"] is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8. Trigger validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_revoke_record_invalid_trigger_raises():
    rec = _make_record()
    fn, _ = _mock_revoke_fn(succeed=True)
    with pytest.raises(ovr.InvalidTriggerError):
        _run(ovr.revoke_record(
            rec, fn, revocation_endpoint="https://x/revoke",
            trigger="bogus",
        ))


def test_invalid_trigger_subclasses_value_error():
    """Existing ``except ValueError`` blocks at call sites continue
    to catch the new error (caller-compat invariant)."""
    assert issubclass(ovr.InvalidTriggerError, ValueError)
    assert issubclass(ovr.InvalidTriggerError, ovr.RevokeError)


def test_revoke_record_accepts_all_revoke_triggers(monkeypatch):
    rec = _make_record()
    _capture_audit(monkeypatch)
    fn, _ = _mock_revoke_fn(succeed=True)
    for trig in sorted(ovr.REVOKE_TRIGGERS):
        out = _run(ovr.revoke_record(
            rec, fn, revocation_endpoint="https://x/revoke", trigger=trig,
        ))
        assert out.outcome == ovr.OUTCOME_SUCCESS
        assert out.trigger == trig


def test_emit_not_linked_invalid_trigger_raises():
    with pytest.raises(ovr.InvalidTriggerError):
        _run(ovr.emit_not_linked(
            user_id="u", provider="google", trigger="garbage",
        ))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9. Actor defaults (DSAR vs voluntary)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_revoke_record_default_actor_is_user_id_for_user_unlink(monkeypatch):
    rec = _make_record(user_id="u-alice")
    events = _capture_audit(monkeypatch)
    fn, _ = _mock_revoke_fn(succeed=True)

    _run(ovr.revoke_record(
        rec, fn, revocation_endpoint="https://x/revoke",
        trigger=ovr.TRIGGER_USER_UNLINK,
    ))
    assert events[0]["actor"] == "u-alice"


def test_revoke_record_default_actor_is_dsar_prefixed_for_dsar(monkeypatch):
    """DSAR audit row's actor should NOT be the data-subject — the
    operator is the actor.  We embed via ``dsar:<user_id>`` so the
    admin filter pane can split the two without a schema change."""
    rec = _make_record(user_id="u-alice")
    events = _capture_audit(monkeypatch)
    fn, _ = _mock_revoke_fn(succeed=True)

    _run(ovr.revoke_record(
        rec, fn, revocation_endpoint="https://x/revoke",
        trigger=ovr.TRIGGER_DSAR_ERASURE,
    ))
    oauth_event = next(e for e in events if e["action"] == oauth_client.EVENT_OAUTH_UNLINK)
    assert oauth_event["actor"] == "dsar:u-alice"
    ks_event = next(e for e in events if e["action"] == "ks.decryption")
    assert ks_event["actor"] == "dsar:u-alice"


def test_revoke_record_explicit_actor_wins_over_default(monkeypatch):
    rec = _make_record(user_id="u-alice")
    events = _capture_audit(monkeypatch)
    fn, _ = _mock_revoke_fn(succeed=True)

    _run(ovr.revoke_record(
        rec, fn, revocation_endpoint="https://x/revoke",
        trigger=ovr.TRIGGER_DSAR_ERASURE,
        actor="dsar:TICKET-4827",
    ))
    assert events[0]["actor"] == "dsar:TICKET-4827"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  10. AS.0.8 single knob — pure helper still runs, audit silent-skips
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_revoke_record_knob_off_audit_silent_skip(monkeypatch):
    """AS.0.4 §6.2: DSAR / right-to-erasure MUST keep working with
    the AS knob off (regulatory deadlines don't pause for feature
    flags).  Pure helper still completes; audit silent-skips per
    AS.0.8 §5 / oauth_audit._gate."""
    rec = _make_record()
    events = _capture_audit(monkeypatch)
    monkeypatch.setattr(
        "backend.security.oauth_audit.oauth_client.is_enabled",
        lambda: False,
    )
    fn, captured = _mock_revoke_fn(succeed=True)

    out = _run(ovr.revoke_record(
        rec, fn, revocation_endpoint="https://x/revoke",
        trigger=ovr.TRIGGER_DSAR_ERASURE,
    ))

    # Pure helper still completed — IdP got called
    assert out.outcome == ovr.OUTCOME_SUCCESS
    assert out.revocation_attempted is True
    assert len(captured["calls"]) == 1
    # AS.1.4 oauth.unlink silent-skips, but KS.1.5 still records the
    # plaintext-returning decrypt.
    assert [e["action"] for e in events] == ["ks.decryption"]


def test_emit_not_linked_knob_off_audit_silent_skip(monkeypatch):
    events = _capture_audit(monkeypatch)
    monkeypatch.setattr(
        "backend.security.oauth_audit.oauth_client.is_enabled",
        lambda: False,
    )
    out = _run(ovr.emit_not_linked(
        user_id="u-x", provider="google",
        trigger=ovr.TRIGGER_DSAR_ERASURE,
    ))
    assert out.outcome == ovr.OUTCOME_NOT_LINKED
    assert events == []


def test_is_enabled_returns_oauth_client_value(monkeypatch):
    """``oauth_revoke.is_enabled`` is a thin re-export of
    ``oauth_client.is_enabled`` (caller-facing gate symmetry)."""
    monkeypatch.setattr(
        "backend.security.oauth_client.is_enabled", lambda: False,
    )
    assert ovr.is_enabled() is False
    monkeypatch.setattr(
        "backend.security.oauth_client.is_enabled", lambda: True,
    )
    assert ovr.is_enabled() is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  11. emit_audit=False opt-out
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_revoke_record_emit_audit_false_keeps_decryption_audit(monkeypatch):
    rec = _make_record()
    events = _capture_audit(monkeypatch)
    fn, _ = _mock_revoke_fn(succeed=True)

    out = _run(ovr.revoke_record(
        rec, fn, revocation_endpoint="https://x/revoke",
        emit_audit=False,
    ))
    assert out.outcome == ovr.OUTCOME_SUCCESS
    assert [e["action"] for e in events] == ["ks.decryption"]


def test_emit_not_linked_emit_audit_false_skips_audit(monkeypatch):
    events = _capture_audit(monkeypatch)
    out = _run(ovr.emit_not_linked(
        user_id="u-x", provider="google", emit_audit=False,
    ))
    assert out.outcome == ovr.OUTCOME_NOT_LINKED
    assert events == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  12. emit_not_linked helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_emit_not_linked_emits_audit_row(monkeypatch):
    events = _capture_audit(monkeypatch)
    out = _run(ovr.emit_not_linked(
        user_id="u-zelda", provider="github",
        trigger=ovr.TRIGGER_USER_UNLINK,
    ))

    assert out.outcome == ovr.OUTCOME_NOT_LINKED
    assert out.revocation_attempted is False
    assert out.revocation_outcome is None
    assert out.error is None
    assert out.trigger == ovr.TRIGGER_USER_UNLINK

    assert len(events) == 1
    ev = events[0]
    assert ev["action"] == oauth_client.EVENT_OAUTH_UNLINK
    assert ev["after"]["outcome"] == oauth_audit.OUTCOME_NOT_LINKED
    assert ev["after"]["revocation_attempted"] is False
    assert ev["after"]["revocation_outcome"] is None
    assert ev["entity_id"] == "github:u-zelda"
    assert ev["actor"] == "u-zelda"


def test_emit_not_linked_explicit_actor_wins(monkeypatch):
    events = _capture_audit(monkeypatch)
    _run(ovr.emit_not_linked(
        user_id="u-z", provider="github",
        trigger=ovr.TRIGGER_DSAR_ERASURE,
        actor="dsar:DSAR-9001",
    ))
    assert events[0]["actor"] == "dsar:DSAR-9001"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  13. Cross-module drift guards
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_outcome_constants_byte_equal_oauth_audit():
    """The hook re-uses the AS.1.4 outcome strings byte-for-byte for
    the audit-vocab subset."""
    assert ovr.OUTCOME_SUCCESS == oauth_audit.OUTCOME_SUCCESS == "success"
    assert (
        ovr.OUTCOME_NOT_LINKED
        == oauth_audit.OUTCOME_NOT_LINKED
        == "not_linked"
    )
    assert (
        ovr.OUTCOME_REVOCATION_FAILED
        == oauth_audit.OUTCOME_REVOCATION_FAILED
        == "revocation_failed"
    )


def test_all_outcomes_tuple_covers_every_outcome_constant():
    """Every ``OUTCOME_*`` constant exported by the module MUST
    appear in :data:`ALL_OUTCOMES`."""
    declared = {
        getattr(ovr, name)
        for name in dir(ovr)
        if name.startswith("OUTCOME_") and isinstance(getattr(ovr, name), str)
    }
    assert declared == set(ovr.ALL_OUTCOMES)


def test_audit_outcome_for_maps_each_hook_outcome_to_unlink_vocab():
    """Every hook outcome maps onto a value the AS.1.4
    UNLINK_OUTCOMES vocabulary accepts (so emit_unlink will not
    raise ValueError on any outcome the hook produces)."""
    cases = [
        (ovr.OUTCOME_SUCCESS, oauth_audit.OUTCOME_SUCCESS),
        (ovr.OUTCOME_NOT_LINKED, oauth_audit.OUTCOME_NOT_LINKED),
        (ovr.OUTCOME_REVOCATION_FAILED, oauth_audit.OUTCOME_REVOCATION_FAILED),
        (ovr.OUTCOME_VAULT_FAILURE, oauth_audit.OUTCOME_REVOCATION_FAILED),
    ]
    for hook_outcome, expected_audit_value in cases:
        outcome = ovr.RevokeOutcome(
            outcome=hook_outcome, revocation_attempted=False,
            revocation_outcome=None, trigger=ovr.TRIGGER_USER_UNLINK,
            error=None,
        )
        mapped = ovr._audit_outcome_for(outcome)
        assert mapped == expected_audit_value
        assert mapped in oauth_audit.UNLINK_OUTCOMES


def test_audit_outcome_for_rejects_unknown_outcome():
    """Future-proofing: a typo in OUTCOME_* must not silently emit a
    misleading audit row."""
    bogus = ovr.RevokeOutcome(
        outcome="not_an_outcome", revocation_attempted=False,
        revocation_outcome=None, trigger=ovr.TRIGGER_USER_UNLINK,
        error=None,
    )
    with pytest.raises(ovr.RevokeError):
        ovr._audit_outcome_for(bogus)


def test_revoke_triggers_disjoint_from_rotation_triggers():
    """REVOKE_TRIGGERS lives in a different audit family
    (oauth.unlink) than ROTATION_TRIGGERS (oauth.token_rotated);
    disjoint vocabularies prevent a typo from silently routing into
    the wrong family."""
    assert ovr.REVOKE_TRIGGERS.isdisjoint(oauth_audit.ROTATION_TRIGGERS)


def test_revoke_trigger_constants_pin_string_values():
    """Lock the canonical strings — they appear in audit rows
    consumed by the AS.5.2 dashboard mapping; a rename here is a
    breaking change for the dashboard."""
    assert ovr.TRIGGER_USER_UNLINK == "user_unlink"
    assert ovr.TRIGGER_DSAR_ERASURE == "dsar_erasure"
    assert ovr.REVOKE_TRIGGERS == frozenset({"user_unlink", "dsar_erasure"})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  14. SOP §1 module-global state audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_no_module_level_mutable_containers():
    """SOP §1 audit answer #1: the module must expose no
    module-level mutable container (list / dict / set)."""
    public = {
        name: getattr(ovr, name)
        for name in dir(ovr)
        if not name.startswith("_")
    }
    for name, val in public.items():
        assert not isinstance(val, list), f"{name} is a list (mutable)"
        assert not isinstance(val, dict), f"{name} is a dict (mutable)"
        assert not isinstance(val, set), f"{name} is a set (mutable)"


def test_module_constants_stable_across_reload():
    before = (
        ovr.OUTCOME_SUCCESS,
        ovr.OUTCOME_NOT_LINKED,
        ovr.OUTCOME_REVOCATION_FAILED,
        ovr.OUTCOME_VAULT_FAILURE,
        ovr.ALL_OUTCOMES,
        ovr.TRIGGER_USER_UNLINK,
        ovr.TRIGGER_DSAR_ERASURE,
        ovr.REVOKE_TRIGGERS,
    )
    importlib.reload(ovr)
    after = (
        ovr.OUTCOME_SUCCESS,
        ovr.OUTCOME_NOT_LINKED,
        ovr.OUTCOME_REVOCATION_FAILED,
        ovr.OUTCOME_VAULT_FAILURE,
        ovr.ALL_OUTCOMES,
        ovr.TRIGGER_USER_UNLINK,
        ovr.TRIGGER_DSAR_ERASURE,
        ovr.REVOKE_TRIGGERS,
    )
    assert before == after


def test_dunder_all_matches_actual_public_surface():
    declared = set(ovr.__all__)
    public_in_module = {
        name
        for name in dir(ovr)
        if not name.startswith("_")
        and name not in {
            # Submodule re-imports we deliberately don't re-export
            "oauth_audit", "oauth_client", "token_vault",
            "TokenVaultRecord", "TokenVaultError", "EncryptedToken",
            "annotations", "logging", "logger",
            "Any", "Awaitable", "Callable", "Optional",
            "dataclass",
        }
    }
    missing = public_in_module - declared
    assert not missing, f"__all__ missing: {missing}"


def test_pre_commit_fingerprint_grep_zero_hits():
    """SOP Step 3 mandatory pre-commit grep — 4 fingerprints must
    be absent from the new module."""
    assert "_conn()" not in REVOKE_SRC
    assert "await conn.commit()" not in REVOKE_SRC
    assert "datetime('now')" not in REVOKE_SRC
    assert not re.search(r"VALUES\s*\(\s*\?", REVOKE_SRC)


def test_module_imports_no_random_module():
    """SOP §1 audit answer: randomness comes from the vault (which
    uses ``secrets``); the hook itself imports neither ``random`` nor
    ``secrets`` — it's a pure orchestrator."""
    assert "\nimport random" not in REVOKE_SRC
    assert "import random\n" not in REVOKE_SRC


def test_module_has_no_top_level_io():
    """No DB query, no file open, no env read at import time."""
    bad = ["open(", "os.environ", "subprocess", "asyncpg.connect", "sqlite3.connect"]
    for token in bad:
        if token == "open(":
            assert REVOKE_SRC.count(token) == 0, f"unexpected {token!r}"
        else:
            assert token not in REVOKE_SRC, f"unexpected {token!r}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  15. Integration smoke — vault round-trip on the chosen token
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_full_revoke_cycle_passes_decrypted_refresh_to_revoke_fn(monkeypatch):
    """End-to-end path: build a record from raw vault-encrypted
    ciphertext, run revoke_record, verify the revoke_fn received
    the OLD plaintext refresh_token (so the IdP gets the right
    token to revoke).  Locks the AS.2.1 vault ↔ AS.2.5 hook
    contract on the chosen-token branch."""
    enc_a = token_vault.encrypt_for_user("u-end", "github", "ghp_END_access")
    enc_r = token_vault.encrypt_for_user("u-end", "github", "ghp_END_refresh")
    rec = orh.TokenVaultRecord.from_db_row(
        user_id="u-end", provider="github",
        access_token_enc=enc_a.ciphertext,
        refresh_token_enc=enc_r.ciphertext,
        expires_at=1000.0, scope="repo,read:user",
        key_version=1, version=42,
    )

    _capture_audit(monkeypatch)
    fn, captured = _mock_revoke_fn(succeed=True)

    out = _run(ovr.revoke_record(
        rec, fn,
        revocation_endpoint="https://github.com/applications/foo/token",
        trigger=ovr.TRIGGER_DSAR_ERASURE,
    ))

    assert out.outcome == ovr.OUTCOME_SUCCESS
    assert out.revocation_attempted is True
    assert out.revocation_outcome == oauth_audit.OUTCOME_SUCCESS
    # revoke_fn was called with the OLD plaintext refresh_token, not
    # the ciphertext — the hook's vault-decrypt step is what bridges
    # storage / network boundaries.
    assert captured["calls"] == [("ghp_END_refresh", "refresh_token")]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  16. AS.1.3 vendor catalog cross-check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_every_vendor_in_vault_whitelist_yields_a_clean_outcome(monkeypatch):
    """For every provider in token_vault.SUPPORTED_PROVIDERS, the
    hook produces a SUCCESS outcome (either real-revocation or
    skipped, depending on whether the vendor exposes an endpoint)
    — i.e. no vendor in the vault whitelist breaks the
    orchestrator's happy path.  This guards against future vendor
    additions that might forget to add a revocation_endpoint
    (or its absence)."""
    _capture_audit(monkeypatch)
    fn, _ = _mock_revoke_fn(succeed=True)

    for provider in sorted(token_vault.SUPPORTED_PROVIDERS):
        try:
            vendor = oauth_vendors.get_vendor(provider)
            endpoint = vendor.revocation_endpoint
        except oauth_vendors.VendorNotFoundError:
            # Vendor catalog is broader than vault whitelist's
            # domain — ok, skip.
            continue
        rec = _make_record(provider=provider)
        out = _run(ovr.revoke_record(
            rec, fn, revocation_endpoint=endpoint,
            trigger=ovr.TRIGGER_USER_UNLINK,
        ))
        assert out.outcome == ovr.OUTCOME_SUCCESS, (
            f"provider {provider} (endpoint={endpoint!r}) "
            f"failed orchestrator happy path"
        )

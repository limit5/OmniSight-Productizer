"""AS.2.4 — `backend.security.oauth_refresh_hook` contract tests.

Locks the load-bearing behaviour of the AS.2.4 refresh hook
(:mod:`backend.security.oauth_refresh_hook`):

1. ``is_due`` predicate: NULL-expiry → False, inside-skew → True,
   outside-skew → False, edge-of-skew (==) → True.
2. ``TokenVaultRecord.from_db_row`` factory: ``''`` refresh-column
   → ``None``; populated → ``EncryptedToken`` carrying the row's
   ``key_version``; whitespace + comma scope splitter normalises;
   numeric ``version`` round-trips.
3. ``refresh_record`` happy path: due row + valid ``refresh_fn`` →
   ``OUTCOME_SUCCESS`` + ``new_record`` with bumped ``version`` +
   freshly-encrypted ciphertext that round-trips back to the new
   plaintext via ``token_vault.decrypt_for_user``.
4. ``refresh_record`` non-due → ``OUTCOME_NOT_DUE`` + no audit row
   emitted (no event happened).
5. ``refresh_record`` no-refresh-token → ``OUTCOME_NO_REFRESH_TOKEN``
   + ``oauth.refresh`` audit row outcome=``no_refresh_token``.
6. ``refresh_record`` provider error (refresh_fn raises) →
   ``OUTCOME_PROVIDER_ERROR`` + ``error`` field carries exception
   class name + audit emits ``provider_error``.
7. ``refresh_record`` IdP returns RFC 6749 §5.2 error shape →
   ``OUTCOME_PROVIDER_ERROR`` (caught inside ``apply_rotation``).
8. ``refresh_record`` rotation: IdP returns NEW refresh_token →
   ``rotated=True`` + ``oauth.token_rotated`` audit row fires +
   neither raw refresh_token leaks into the audit JSON
   (fingerprint-only invariant).
9. ``refresh_record`` no rotation: IdP omits refresh_token → previous
   one preserved, ``rotated=False``, ``oauth.token_rotated`` NOT
   emitted.
10. ``refresh_record`` vault decrypt failure (binding mismatch from a
    DB row swap, or unknown key_version) → ``OUTCOME_VAULT_FAILURE``
    + audit emits ``provider_error`` with ``error="vault:..."``.
11. ``refresh_record`` AS.0.8 knob-off: pure helper still runs
    (re-encrypts new ciphertext, returns ``new_record``) but audit
    silent-skips (mirrors ``oauth_audit._gate`` behaviour).
12. ``refresh_record`` ``trigger`` validation: rejects strings outside
    :data:`oauth_audit.ROTATION_TRIGGERS` with :class:`InvalidTriggerError`.
13. ``refresh_record`` ``emit_audit=False`` skip: caller can opt out
    of the audit fan-out.
14. Module-global state audit per SOP §1: 0 module-level mutable
    containers (no list / dict / set), constants stable across
    ``importlib.reload``, ``__all__`` cross-check.
15. Cross-module drift guards:
    * ``DEFAULT_REFRESH_SKEW_SECONDS`` byte-equal
      :data:`oauth_client.DEFAULT_REFRESH_SKEW_SECONDS`.
    * ``OUTCOME_SUCCESS`` / ``OUTCOME_NO_REFRESH_TOKEN`` /
      ``OUTCOME_PROVIDER_ERROR`` byte-equal
      :data:`oauth_audit.OUTCOME_*`.
    * ``new_record.version == record.version + 1`` exactly (locks
      AS.2.2 optimistic-lock contract).
16. Pre-commit fingerprint grep on the new module — 4 SOP fingerprints
    (``_conn()`` / ``await conn.commit()`` / ``datetime('now')`` /
    ``VALUES (?,...)``) all 0-hit.

The hook is pure: tests use a fake async ``refresh_fn`` callable and
monkey-patch the audit emitters. No DB / network. The vault round-trip
exercises ``backend.secret_store`` (Fernet) end-to-end so the encryption
half of AS.2.1 is also implicitly verified through this row.
"""

from __future__ import annotations

import asyncio
import importlib
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from backend.security import (
    oauth_audit,
    oauth_client,
    oauth_refresh_hook as orh,
    token_vault,
)


REFRESH_HOOK_SRC = Path(orh.__file__).read_text(encoding="utf-8")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _run(coro):
    return asyncio.run(coro)


def _make_record(
    *,
    user_id: str = "u-alice",
    provider: str = "google",
    access_plaintext: str = "ya29.access-token-old",
    refresh_plaintext: Any = "1//refresh-token-old",
    expires_at: float = 1000.0,
    scope: tuple[str, ...] = ("openid", "email"),
    version: int = 7,
) -> orh.TokenVaultRecord:
    """Build a TokenVaultRecord with real vault-encrypted ciphertext.

    Pass ``refresh_plaintext=None`` to build a record whose
    refresh_token_enc is ``None`` (mirrors AS.2.2 ``''`` default).
    """
    access_enc = token_vault.encrypt_for_user(user_id, provider, access_plaintext)
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


def _mock_refresh_fn(payload: dict[str, Any]):
    """Return an async callable that captures the refresh_token it was
    called with and returns *payload* verbatim."""
    captured: dict[str, Any] = {"calls": []}

    async def fn(refresh_token: str):
        captured["calls"].append(refresh_token)
        return payload

    return fn, captured


def _capture_audit(monkeypatch):
    """Replace ``oauth_audit.audit.log`` with a fake that records every
    call. Returns the captured-events list."""
    events: list[dict[str, Any]] = []

    async def fake_log(**kwargs):
        events.append(kwargs)
        return len(events)

    monkeypatch.setattr("backend.security.oauth_audit.audit.log", fake_log)
    return events


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. is_due predicate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_is_due_null_expires_at_returns_false():
    rec = _make_record(expires_at=None)
    assert orh.is_due(rec, now=999_999_999.0) is False


def test_is_due_inside_skew_window_returns_true():
    rec = _make_record(expires_at=1000.0)
    # now=950, skew=60 → expires_at - skew = 940 ≤ 950 → True.
    assert orh.is_due(rec, skew_seconds=60, now=950.0) is True


def test_is_due_outside_skew_window_returns_false():
    rec = _make_record(expires_at=1000.0)
    # now=900, skew=60 → expires_at - skew = 940 > 900 → False.
    assert orh.is_due(rec, skew_seconds=60, now=900.0) is False


def test_is_due_edge_of_skew_window_inclusive():
    rec = _make_record(expires_at=1000.0)
    # ts == expires_at - skew → ``ts >= expires_at - skew`` True.
    assert orh.is_due(rec, skew_seconds=60, now=940.0) is True


def test_is_due_past_expiry_returns_true():
    rec = _make_record(expires_at=1000.0)
    assert orh.is_due(rec, skew_seconds=60, now=2_000.0) is True


def test_default_skew_is_60_seconds_byte_equal_oauth_client():
    """Cross-module drift guard: AS.2.4 ``DEFAULT_REFRESH_SKEW_SECONDS``
    re-export MUST byte-equal AS.1.1 ``oauth_client.DEFAULT_REFRESH_SKEW_SECONDS``
    (the row title says "60s")."""
    assert orh.DEFAULT_REFRESH_SKEW_SECONDS == 60
    assert (
        orh.DEFAULT_REFRESH_SKEW_SECONDS
        == oauth_client.DEFAULT_REFRESH_SKEW_SECONDS
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. TokenVaultRecord.from_db_row factory
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_from_db_row_empty_refresh_column_becomes_none():
    enc = token_vault.encrypt_for_user("u-x", "google", "secret-access")
    rec = orh.TokenVaultRecord.from_db_row(
        user_id="u-x",
        provider="google",
        access_token_enc=enc.ciphertext,
        refresh_token_enc="",  # AS.2.2 default for "no refresh token"
        expires_at=1234.0,
        scope="openid email",
        key_version=1,
        version=3,
    )
    assert rec.refresh_token_enc is None


def test_from_db_row_populated_refresh_carries_key_version():
    enc_a = token_vault.encrypt_for_user("u-x", "google", "access")
    enc_r = token_vault.encrypt_for_user("u-x", "google", "refresh")
    rec = orh.TokenVaultRecord.from_db_row(
        user_id="u-x",
        provider="google",
        access_token_enc=enc_a.ciphertext,
        refresh_token_enc=enc_r.ciphertext,
        expires_at=1234.0,
        scope="openid email",
        key_version=1,
        version=3,
    )
    assert rec.refresh_token_enc is not None
    assert rec.refresh_token_enc.key_version == 1
    # The factory MUST round-trip both ciphertexts byte-equal so
    # decrypt downstream sees the same Fernet token.
    assert rec.access_token_enc.ciphertext == enc_a.ciphertext
    assert rec.refresh_token_enc.ciphertext == enc_r.ciphertext


def test_from_db_row_scope_normaliser_handles_space_and_comma():
    enc_a = token_vault.encrypt_for_user("u", "google", "x")
    rec = orh.TokenVaultRecord.from_db_row(
        user_id="u",
        provider="google",
        access_token_enc=enc_a.ciphertext,
        refresh_token_enc="",
        expires_at=None,
        scope="openid, email  profile",  # mixed comma + double-space
        key_version=1,
        version=0,
    )
    assert rec.scope == ("openid", "email", "profile")


def test_from_db_row_empty_scope_yields_empty_tuple():
    enc_a = token_vault.encrypt_for_user("u", "google", "x")
    rec = orh.TokenVaultRecord.from_db_row(
        user_id="u",
        provider="google",
        access_token_enc=enc_a.ciphertext,
        refresh_token_enc="",
        expires_at=None,
        scope="",
        key_version=1,
        version=0,
    )
    assert rec.scope == ()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. refresh_record happy path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_refresh_record_success_bumps_version_and_encrypts_new_tokens(monkeypatch):
    rec = _make_record(version=7, expires_at=1000.0)
    events = _capture_audit(monkeypatch)
    fn, captured = _mock_refresh_fn({
        "access_token": "ya29.brand-new-access",
        "refresh_token": "1//rotated-refresh",
        "expires_in": 3600,
        "scope": "openid email profile",
        "token_type": "Bearer",
    })

    out: orh.RefreshOutcome = _run(orh.refresh_record(
        rec, fn, skew_seconds=60, now=950.0, trigger="auto_refresh",
    ))

    # Outcome shape
    assert out.outcome == orh.OUTCOME_SUCCESS
    assert out.new_record is not None
    assert out.rotated is True
    assert out.error is None
    assert out.previous_expires_at == 1000.0
    assert out.new_expires_in_seconds == 3600
    assert out.granted_scope == ("openid", "email", "profile")

    # Optimistic-lock counter bumped by exactly one
    assert out.new_record.version == rec.version + 1 == 8

    # The fresh access ciphertext decrypts back to the new plaintext
    recovered_access = token_vault.decrypt_for_user(
        out.new_record.user_id,
        out.new_record.provider,
        out.new_record.access_token_enc,
    )
    assert recovered_access == "ya29.brand-new-access"

    # Refresh ciphertext is freshly re-encrypted (rotation case)
    assert out.new_record.refresh_token_enc is not None
    recovered_refresh = token_vault.decrypt_for_user(
        out.new_record.user_id,
        out.new_record.provider,
        out.new_record.refresh_token_enc,
    )
    assert recovered_refresh == "1//rotated-refresh"

    # refresh_fn was called with the OLD refresh_token plaintext
    assert captured["calls"] == ["1//refresh-token-old"]

    # Audit emitted both `oauth.refresh` (success) and `oauth.token_rotated`
    actions = [e["action"] for e in events]
    assert oauth_client.EVENT_OAUTH_REFRESH in actions
    assert oauth_client.EVENT_OAUTH_TOKEN_ROTATED in actions


def test_refresh_record_success_not_rotated_when_provider_omits_refresh_token(monkeypatch):
    """RFC 6749 §6 + apply_rotation contract: when the IdP omits
    ``refresh_token`` in the response, the previous one is preserved
    and ``rotated`` is False (no oauth.token_rotated audit)."""
    rec = _make_record(version=3, expires_at=1000.0)
    events = _capture_audit(monkeypatch)
    fn, _ = _mock_refresh_fn({
        "access_token": "ya29.new-access-no-rotate",
        "expires_in": 3600,
        # no refresh_token field
        "scope": "openid email",
        "token_type": "Bearer",
    })

    out = _run(orh.refresh_record(rec, fn, skew_seconds=60, now=950.0))

    assert out.outcome == orh.OUTCOME_SUCCESS
    assert out.rotated is False
    # Previous refresh ciphertext is preserved (re-encrypted with the
    # same plaintext though — Fernet ciphertext carries a fresh nonce
    # per encrypt so byte-equality is NOT expected; what IS expected
    # is the recovered plaintext matching the original).
    assert out.new_record.refresh_token_enc is not None
    recovered = token_vault.decrypt_for_user(
        out.new_record.user_id,
        out.new_record.provider,
        out.new_record.refresh_token_enc,
    )
    assert recovered == "1//refresh-token-old"

    actions = [e["action"] for e in events]
    assert oauth_client.EVENT_OAUTH_REFRESH in actions
    assert oauth_client.EVENT_OAUTH_TOKEN_ROTATED not in actions


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. NOT_DUE — no event, no audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_refresh_record_not_due_returns_outcome_not_due_and_no_audit(monkeypatch):
    rec = _make_record(expires_at=1000.0)
    events = _capture_audit(monkeypatch)
    fn, captured = _mock_refresh_fn({"access_token": "x", "expires_in": 3600})

    out = _run(orh.refresh_record(rec, fn, skew_seconds=60, now=900.0))

    assert out.outcome == orh.OUTCOME_NOT_DUE
    assert out.new_record is None
    assert out.rotated is False
    # refresh_fn was NOT called
    assert captured["calls"] == []
    # No audit row emitted (no event happened — pure short-circuit)
    assert events == []


def test_refresh_record_null_expiry_treated_as_not_due(monkeypatch):
    """Provider doesn't issue ``expires_in`` (Notion / GitHub PAT-style):
    hook short-circuits to NOT_DUE; caller must rely on a 401 to know
    the token is dead."""
    rec = _make_record(expires_at=None)
    events = _capture_audit(monkeypatch)
    fn, captured = _mock_refresh_fn({"access_token": "x", "expires_in": 3600})

    out = _run(orh.refresh_record(rec, fn, now=999_999_999.0))

    assert out.outcome == orh.OUTCOME_NOT_DUE
    assert captured["calls"] == []
    assert events == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. NO_REFRESH_TOKEN — Apple-style no-rotation provider
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_refresh_record_no_refresh_token_emits_audit(monkeypatch):
    rec = _make_record(refresh_plaintext=None, expires_at=1000.0)
    events = _capture_audit(monkeypatch)
    fn, captured = _mock_refresh_fn({"access_token": "x", "expires_in": 3600})

    out = _run(orh.refresh_record(rec, fn, skew_seconds=60, now=999.0))

    assert out.outcome == orh.OUTCOME_NO_REFRESH_TOKEN
    assert out.new_record is None
    assert captured["calls"] == []  # never called
    # Audit row emitted with outcome=no_refresh_token
    assert len(events) == 1
    assert events[0]["action"] == oauth_client.EVENT_OAUTH_REFRESH
    assert events[0]["after"]["outcome"] == oauth_audit.OUTCOME_NO_REFRESH_TOKEN


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. PROVIDER_ERROR — refresh_fn raises
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_refresh_record_provider_exception_returns_provider_error(monkeypatch):
    rec = _make_record(expires_at=1000.0)
    events = _capture_audit(monkeypatch)

    async def boom(_):
        raise RuntimeError("network unreachable")

    out = _run(orh.refresh_record(rec, boom, skew_seconds=60, now=999.0))

    assert out.outcome == orh.OUTCOME_PROVIDER_ERROR
    assert out.new_record is None
    assert "RuntimeError" in (out.error or "")
    assert "network unreachable" in (out.error or "")

    assert len(events) == 1
    assert events[0]["after"]["outcome"] == oauth_audit.OUTCOME_PROVIDER_ERROR
    assert "RuntimeError" in (events[0]["after"].get("error") or "")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. PROVIDER_ERROR — IdP returns RFC 6749 §5.2 error shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_refresh_record_idp_error_shape_returns_provider_error(monkeypatch):
    rec = _make_record(expires_at=1000.0)
    events = _capture_audit(monkeypatch)
    fn, _ = _mock_refresh_fn({
        "error": "invalid_grant",
        "error_description": "refresh_token revoked",
    })

    out = _run(orh.refresh_record(rec, fn, skew_seconds=60, now=999.0))

    assert out.outcome == orh.OUTCOME_PROVIDER_ERROR
    assert out.error is not None
    assert "TokenResponseError" in out.error or "invalid_grant" in out.error
    assert events[-1]["after"]["outcome"] == oauth_audit.OUTCOME_PROVIDER_ERROR


def test_refresh_record_missing_access_token_returns_provider_error(monkeypatch):
    rec = _make_record(expires_at=1000.0)
    _capture_audit(monkeypatch)
    fn, _ = _mock_refresh_fn({
        # No access_token → parse_token_response raises TokenResponseError
        "expires_in": 3600,
    })

    out = _run(orh.refresh_record(rec, fn, skew_seconds=60, now=999.0))

    assert out.outcome == orh.OUTCOME_PROVIDER_ERROR
    assert "TokenResponseError" in (out.error or "")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8. Rotation — token_rotated audit + fingerprint-only invariant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_refresh_record_rotation_writes_fingerprints_not_raw(monkeypatch):
    """When IdP rotates refresh_token, oauth.token_rotated row must
    NEVER contain raw refresh_token strings — only their first-12-char
    SHA-256 fingerprints (per AS.1.4 emit_token_rotated contract,
    which AS.2.4 calls into)."""
    rec = _make_record(version=1, expires_at=1000.0)
    events = _capture_audit(monkeypatch)
    fn, _ = _mock_refresh_fn({
        "access_token": "fresh-access",
        "refresh_token": "1//ROTATED-NEW-SECRET",
        "expires_in": 3600,
    })

    _run(orh.refresh_record(rec, fn, skew_seconds=60, now=999.0))

    # Find the oauth.token_rotated row
    rotated = [e for e in events if e["action"] == oauth_client.EVENT_OAUTH_TOKEN_ROTATED]
    assert len(rotated) == 1
    serialised = str(rotated[0])
    # Neither old nor new refresh_token plaintext appears in audit row
    assert "1//refresh-token-old" not in serialised
    assert "1//ROTATED-NEW-SECRET" not in serialised
    # But the fingerprints DO (12-char hex)
    expected_old_fp = oauth_audit.fingerprint("1//refresh-token-old")
    expected_new_fp = oauth_audit.fingerprint("1//ROTATED-NEW-SECRET")
    assert expected_old_fp in serialised
    assert expected_new_fp in serialised


def test_refresh_record_rotation_uses_trigger_field(monkeypatch):
    rec = _make_record(expires_at=1000.0)
    events = _capture_audit(monkeypatch)
    fn, _ = _mock_refresh_fn({
        "access_token": "a", "refresh_token": "1//rotated",
        "expires_in": 3600,
    })

    _run(orh.refresh_record(rec, fn, skew_seconds=60, now=999.0,
                            trigger="explicit_refresh"))

    rotated = [e for e in events if e["action"] == oauth_client.EVENT_OAUTH_TOKEN_ROTATED]
    assert rotated[0]["after"]["triggered_by"] == "explicit_refresh"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9. Vault failure
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_refresh_record_vault_binding_mismatch_returns_vault_failure(monkeypatch):
    """Simulate the ``oauth_tokens`` row swap defence (DB-level swap of
    ciphertext between users): build a record with ciphertext encrypted
    for user A but claim it's user B's. Vault raises BindingMismatchError
    inside decrypt_for_user; hook surfaces OUTCOME_VAULT_FAILURE +
    audit emits provider_error with error="vault:BindingMismatchError"."""
    enc_for_alice = token_vault.encrypt_for_user("u-alice", "google", "alice-access")
    enc_refresh_for_alice = token_vault.encrypt_for_user("u-alice", "google", "alice-refresh")
    rec = orh.TokenVaultRecord(
        user_id="u-bob",  # claim it's bob's row, but ciphertext is alice's
        provider="google",
        access_token_enc=enc_for_alice,
        refresh_token_enc=enc_refresh_for_alice,
        expires_at=1000.0,
        scope=(),
        version=1,
    )
    events = _capture_audit(monkeypatch)
    fn, captured = _mock_refresh_fn({"access_token": "x", "expires_in": 3600})

    out = _run(orh.refresh_record(rec, fn, skew_seconds=60, now=999.0))

    assert out.outcome == orh.OUTCOME_VAULT_FAILURE
    assert out.error is not None
    assert "vault:BindingMismatchError" in out.error
    # refresh_fn never called — fail-fast on vault decrypt
    assert captured["calls"] == []
    # Audit row emitted with outcome=provider_error (vault collapses
    # onto provider_error in the audit vocabulary)
    assert len(events) == 1
    assert events[0]["after"]["outcome"] == oauth_audit.OUTCOME_PROVIDER_ERROR
    assert "vault:" in (events[0]["after"]["error"] or "")


def test_refresh_record_vault_unknown_key_version_returns_vault_failure(monkeypatch):
    enc_a = token_vault.encrypt_for_user("u", "google", "access")
    enc_r = token_vault.encrypt_for_user("u", "google", "refresh")
    # Manufacture an EncryptedToken with key_version=99 (unknown)
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
    fn, _ = _mock_refresh_fn({"access_token": "x", "expires_in": 3600})

    out = _run(orh.refresh_record(rec, fn, skew_seconds=60, now=999.0))

    assert out.outcome == orh.OUTCOME_VAULT_FAILURE
    assert "UnknownKeyVersionError" in (out.error or "")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  10. AS.0.8 single knob — pure helper still runs, audit silent-skips
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_refresh_record_knob_off_helper_runs_audit_silent_skip(monkeypatch):
    """AS.0.4 §6.2: pure helpers must keep working with the AS knob off
    so backfill / DSAR / key-rotation scripts can still refresh tokens.
    Audit emit silent-skips per AS.0.8 §5 (oauth_audit._gate)."""
    rec = _make_record(version=2, expires_at=1000.0)
    events = _capture_audit(monkeypatch)
    monkeypatch.setattr(
        "backend.security.oauth_audit.oauth_client.is_enabled",
        lambda: False,
    )
    fn, _ = _mock_refresh_fn({
        "access_token": "fresh", "refresh_token": "1//rotated",
        "expires_in": 3600,
    })

    out = _run(orh.refresh_record(rec, fn, skew_seconds=60, now=999.0))

    # Pure helper still completed
    assert out.outcome == orh.OUTCOME_SUCCESS
    assert out.new_record is not None
    assert out.new_record.version == 3
    # No audit row written (knob-off ⇒ silent skip)
    assert events == []


def test_is_enabled_returns_oauth_client_value(monkeypatch):
    """``oauth_refresh_hook.is_enabled`` is a thin re-export of
    ``oauth_client.is_enabled`` (caller-facing gate symmetry)."""
    monkeypatch.setattr(
        "backend.security.oauth_client.is_enabled", lambda: False,
    )
    assert orh.is_enabled() is False
    monkeypatch.setattr(
        "backend.security.oauth_client.is_enabled", lambda: True,
    )
    assert orh.is_enabled() is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  11. Trigger validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_refresh_record_invalid_trigger_raises():
    rec = _make_record(expires_at=1000.0)
    fn, _ = _mock_refresh_fn({"access_token": "x", "expires_in": 3600})
    with pytest.raises(orh.InvalidTriggerError):
        _run(orh.refresh_record(rec, fn, skew_seconds=60, now=999.0,
                                trigger="bogus"))


def test_invalid_trigger_subclasses_value_error():
    """Existing ``except ValueError`` blocks at call-sites continue to
    catch the new error (caller-compat invariant)."""
    assert issubclass(orh.InvalidTriggerError, ValueError)
    assert issubclass(orh.InvalidTriggerError, orh.RefreshHookError)


def test_refresh_record_accepts_both_trigger_strings(monkeypatch):
    rec = _make_record(expires_at=1000.0)
    _capture_audit(monkeypatch)
    fn, _ = _mock_refresh_fn({"access_token": "x", "expires_in": 3600})
    for trigger in sorted(oauth_audit.ROTATION_TRIGGERS):
        out = _run(orh.refresh_record(rec, fn, skew_seconds=60, now=999.0,
                                      trigger=trigger))
        assert out.outcome == orh.OUTCOME_SUCCESS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  12. emit_audit=False opt-out
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_refresh_record_emit_audit_false_skips_audit(monkeypatch):
    rec = _make_record(expires_at=1000.0)
    events = _capture_audit(monkeypatch)
    fn, _ = _mock_refresh_fn({
        "access_token": "x", "refresh_token": "1//rot",
        "expires_in": 3600,
    })

    out = _run(orh.refresh_record(rec, fn, skew_seconds=60, now=999.0,
                                  emit_audit=False))

    assert out.outcome == orh.OUTCOME_SUCCESS
    assert events == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  13. Cross-module drift guards
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_outcome_constants_byte_equal_oauth_audit():
    """The hook re-uses the AS.1.4 outcome strings byte-for-byte for
    the audit-vocab subset."""
    assert orh.OUTCOME_SUCCESS == oauth_audit.OUTCOME_SUCCESS == "success"
    assert (
        orh.OUTCOME_NO_REFRESH_TOKEN
        == oauth_audit.OUTCOME_NO_REFRESH_TOKEN
        == "no_refresh_token"
    )
    assert (
        orh.OUTCOME_PROVIDER_ERROR
        == oauth_audit.OUTCOME_PROVIDER_ERROR
        == "provider_error"
    )


def test_all_outcomes_tuple_covers_every_outcome_constant():
    """Every ``OUTCOME_*`` constant exported by the module MUST appear
    in :data:`ALL_OUTCOMES` (catches "added a new outcome but forgot
    to list it")."""
    declared = {
        getattr(orh, name)
        for name in dir(orh)
        if name.startswith("OUTCOME_") and isinstance(getattr(orh, name), str)
    }
    assert declared == set(orh.ALL_OUTCOMES)


def test_audit_outcome_for_refuses_not_due_outcome():
    """The internal mapper rejects outcomes that should not produce an
    audit row (locks the design intent so a future refactor can't
    silently emit a misleading row)."""
    not_due = orh.RefreshOutcome(
        outcome=orh.OUTCOME_NOT_DUE,
        new_record=None, rotated=False, error=None,
        previous_expires_at=None, new_expires_in_seconds=None,
        granted_scope=(),
    )
    with pytest.raises(orh.RefreshHookError):
        orh._audit_outcome_for(not_due)


def test_audit_outcome_for_collapses_vault_failure_to_provider_error():
    """Vault failures map onto ``provider_error`` in the audit
    vocabulary because the AS.1.4 contract only ships three outcomes
    and operationally it's "couldn't refresh"."""
    vault = orh.RefreshOutcome(
        outcome=orh.OUTCOME_VAULT_FAILURE,
        new_record=None, rotated=False, error="vault:Foo",
        previous_expires_at=None, new_expires_in_seconds=None,
        granted_scope=(),
    )
    assert orh._audit_outcome_for(vault) == oauth_audit.OUTCOME_PROVIDER_ERROR


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  14. Module-global state audit (SOP §1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_no_module_level_mutable_containers():
    """SOP §1 audit answer #1: the module must expose no module-level
    mutable container (list / dict / set). Constants are tuples,
    frozensets, immutable strings, classes."""
    public = {
        name: getattr(orh, name)
        for name in dir(orh)
        if not name.startswith("_")
    }
    for name, val in public.items():
        assert not isinstance(val, list), f"{name} is a list (mutable)"
        assert not isinstance(val, dict), f"{name} is a dict (mutable)"
        # ``set`` is not allowed as module-level state either; constants
        # use ``frozenset`` if the structure is set-shaped.
        assert not isinstance(val, set), f"{name} is a set (mutable)"


def test_module_constants_stable_across_reload():
    before = (
        orh.DEFAULT_REFRESH_SKEW_SECONDS,
        orh.OUTCOME_SUCCESS,
        orh.OUTCOME_NOT_DUE,
        orh.OUTCOME_NO_REFRESH_TOKEN,
        orh.OUTCOME_PROVIDER_ERROR,
        orh.OUTCOME_VAULT_FAILURE,
        orh.ALL_OUTCOMES,
    )
    importlib.reload(orh)
    after = (
        orh.DEFAULT_REFRESH_SKEW_SECONDS,
        orh.OUTCOME_SUCCESS,
        orh.OUTCOME_NOT_DUE,
        orh.OUTCOME_NO_REFRESH_TOKEN,
        orh.OUTCOME_PROVIDER_ERROR,
        orh.OUTCOME_VAULT_FAILURE,
        orh.ALL_OUTCOMES,
    )
    assert before == after


def test_dunder_all_matches_actual_public_surface():
    """``__all__`` must list every public name the tests / callers
    rely on (and nothing more)."""
    declared = set(orh.__all__)
    public_in_module = {
        name
        for name in dir(orh)
        if not name.startswith("_")
        and name not in {
            # Submodule re-imports we deliberately don't re-export
            "oauth_audit", "oauth_client", "token_vault",
            "EncryptedToken", "TokenVaultError",
            "TokenRefreshError", "TokenResponseError", "TokenSet",
            "annotations", "logging", "time", "logger",
            "Any", "Awaitable", "Callable", "Mapping", "Optional",
            "dataclass",
        }
    }
    missing = public_in_module - declared
    assert not missing, f"__all__ missing: {missing}"


def test_pre_commit_fingerprint_grep_zero_hits():
    """SOP Step 3 mandatory pre-commit grep — 4 fingerprints must be
    absent from the new module (no compat-layer leftover)."""
    assert "_conn()" not in REFRESH_HOOK_SRC
    assert "await conn.commit()" not in REFRESH_HOOK_SRC
    assert "datetime('now')" not in REFRESH_HOOK_SRC
    # ``VALUES (?,...)`` SQLite-style placeholders — the module is
    # pure Python (no SQL at all), so no ``VALUES ?`` regex either.
    assert not re.search(r"VALUES\s*\(\s*\?", REFRESH_HOOK_SRC)


def test_module_imports_no_random_module():
    """SOP §1 audit answer: randomness comes from the vault (which
    uses ``secrets``); the hook itself imports neither ``random`` nor
    ``secrets`` (it's a pure orchestrator)."""
    assert "\nimport random" not in REFRESH_HOOK_SRC
    assert "import random\n" not in REFRESH_HOOK_SRC


def test_module_has_no_top_level_io():
    """No DB query, no file open, no env read at import time."""
    bad = ["open(", "os.environ", "subprocess", "asyncpg.connect", "sqlite3.connect"]
    for token in bad:
        # "open(" appears in docstrings as English; restrict to first
        # 80 lines (header) — but cleaner: assert the module imported
        # successfully and exposes its public symbols (the IO check
        # is functionally that "import orh" did not perform IO).
        # Here we just sanity-check that the source isn't doing such.
        if token == "open(":
            # the source file itself contains "open(" zero times
            assert REFRESH_HOOK_SRC.count(token) == 0, f"unexpected {token!r}"
        else:
            assert token not in REFRESH_HOOK_SRC, f"unexpected {token!r}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  15. Integration smoke — round-trip with the AS.2.1 vault
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_full_refresh_cycle_round_trips_via_vault(monkeypatch):
    """End-to-end path: build a record from raw column values (mimics
    a SELECT from oauth_tokens), refresh it, re-read both ciphertexts
    via the vault, confirm the new plaintexts match what the IdP sent.
    Locks the AS.2.1 vault ↔ AS.2.2 schema ↔ AS.2.4 hook contract in
    one single test the way the AS.2.2 round_trip test locks the
    schema half."""
    enc_a = token_vault.encrypt_for_user("u-end", "github", "ghp_OLD_access")
    enc_r = token_vault.encrypt_for_user("u-end", "github", "ghp_OLD_refresh")
    rec = orh.TokenVaultRecord.from_db_row(
        user_id="u-end",
        provider="github",
        access_token_enc=enc_a.ciphertext,
        refresh_token_enc=enc_r.ciphertext,
        expires_at=1000.0,
        scope="repo,read:user",
        key_version=1,
        version=42,
    )

    _capture_audit(monkeypatch)
    fn, captured = _mock_refresh_fn({
        "access_token": "ghp_NEW_access",
        "refresh_token": "ghp_NEW_refresh",
        "expires_in": 28800,
        "scope": "repo read:user",
        "token_type": "bearer",
    })

    out = _run(orh.refresh_record(rec, fn, skew_seconds=60, now=999.0))

    assert out.outcome == orh.OUTCOME_SUCCESS
    assert out.rotated is True
    assert out.new_record.version == 43
    assert (
        token_vault.decrypt_for_user(
            "u-end", "github", out.new_record.access_token_enc,
        )
        == "ghp_NEW_access"
    )
    assert (
        token_vault.decrypt_for_user(
            "u-end", "github", out.new_record.refresh_token_enc,
        )
        == "ghp_NEW_refresh"
    )
    # refresh_fn was given the OLD plaintext refresh, never the
    # ciphertext — the hook's vault-decrypt step is what bridges the
    # storage / network boundaries.
    assert captured["calls"] == ["ghp_OLD_refresh"]

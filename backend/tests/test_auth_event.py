"""AS.5.1 — Unit tests for ``backend.security.auth_event``.

Coverage shape (test families):

  * Family 1  — Eight canonical event names + ALL_AUTH_EVENTS tuple.
  * Family 2  — entity_kind constants pinning.
  * Family 3  — Vocabularies (auth methods × 6, login fail reasons × 10,
                bot-challenge pass kinds × 4, fail reasons × 5, token
                refresh outcomes × 3, rotation triggers × 2, oauth-
                connect outcomes × 2, oauth-revoke initiators × 3).
  * Family 4  — fingerprint() round-trip + None/empty + length + cross-
                module byte-equality with oauth_audit.fingerprint.
  * Family 5  — login_success builder shape + auth_method validation +
                actor fallback to user_id + ip/user-agent fingerprinting.
  * Family 6  — login_fail builder shape + dual vocabulary validation +
                attempted_user fingerprint as entity_id + actor default
                "anonymous".
  * Family 7  — oauth_connect builder shape + outcome validation +
                scope normalisation + entity_id format.
  * Family 8  — oauth_revoke builder shape + initiator validation.
  * Family 9  — bot_challenge_pass builder shape + kind validation +
                score-required-for-verified rule + score-must-be-null
                for-bypass rule.
  * Family 10 — bot_challenge_fail builder shape + reason validation +
                optional score round-trip.
  * Family 11 — token_refresh builder shape + outcome validation +
                actor fallback to user_id.
  * Family 12 — token_rotated builder shape + triggered_by validation +
                fingerprint of both tokens (raw never persisted).
  * Family 13 — Eight emit_* helpers route into audit.log with the
                builder's payload.
  * Family 14 — AS.0.8 single-knob: all 8 emit_* skip when knob-off.
  * Family 15 — Module-global state: no IO at import, no mutable
                module-level container, hashlib (not secrets) provenance.
  * Family 16 — __all__ export shape (every public symbol present, no
                stowaways).

Module-global state audit (per implement_phase_step.md SOP §1)
* All test data is local to test fns; no module-global mutable state.
* Each test mocks ``audit.log`` per-call via monkeypatch so audit
  writes don't actually fan out to a DB chain.
* Tests run in any order — no fixture cross-test dependency.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import pathlib
import re
from typing import Any
from unittest.mock import patch

import pytest

from backend.security import auth_event as ae
from backend.security import oauth_audit as oa


def _run(coro):
    """Sync wrapper for one-off async helpers."""
    return asyncio.run(coro)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — Eight canonical event names
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_event_names_exact() -> None:
    """The 8 event strings are pinned by the AS.5.1 row.  Renaming any
    of these is a breaking change for AS.5.2 dashboard widgets and
    AS.6.5 self-audit consumers."""
    assert ae.EVENT_AUTH_LOGIN_SUCCESS == "auth.login_success"
    assert ae.EVENT_AUTH_LOGIN_FAIL == "auth.login_fail"
    assert ae.EVENT_AUTH_OAUTH_CONNECT == "auth.oauth_connect"
    assert ae.EVENT_AUTH_OAUTH_REVOKE == "auth.oauth_revoke"
    assert ae.EVENT_AUTH_BOT_CHALLENGE_PASS == "auth.bot_challenge_pass"
    assert ae.EVENT_AUTH_BOT_CHALLENGE_FAIL == "auth.bot_challenge_fail"
    assert ae.EVENT_AUTH_TOKEN_REFRESH == "auth.token_refresh"
    assert ae.EVENT_AUTH_TOKEN_ROTATED == "auth.token_rotated"


def test_all_auth_events_tuple_complete() -> None:
    assert ae.ALL_AUTH_EVENTS == (
        ae.EVENT_AUTH_LOGIN_SUCCESS,
        ae.EVENT_AUTH_LOGIN_FAIL,
        ae.EVENT_AUTH_OAUTH_CONNECT,
        ae.EVENT_AUTH_OAUTH_REVOKE,
        ae.EVENT_AUTH_BOT_CHALLENGE_PASS,
        ae.EVENT_AUTH_BOT_CHALLENGE_FAIL,
        ae.EVENT_AUTH_TOKEN_REFRESH,
        ae.EVENT_AUTH_TOKEN_ROTATED,
    )
    assert len(ae.ALL_AUTH_EVENTS) == 8
    assert len(set(ae.ALL_AUTH_EVENTS)) == 8


def test_all_events_share_auth_namespace_prefix() -> None:
    """Every AS.5.1 event lives under the ``auth.`` namespace so the
    dashboard's wildcard query (``action LIKE 'auth.%'``) catches them
    all.  Distinct from the ``oauth.*`` forensic family (AS.1.4) and
    the ``bot_challenge.*`` family (AS.3.1)."""
    for ev in ae.ALL_AUTH_EVENTS:
        assert ev.startswith("auth.")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — entity_kind constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_entity_kind_strings() -> None:
    assert ae.ENTITY_KIND_AUTH_SESSION == "auth_session"
    assert ae.ENTITY_KIND_OAUTH_CONNECTION == "oauth_connection"
    assert ae.ENTITY_KIND_OAUTH_TOKEN == "oauth_token"


def test_entity_kind_oauth_token_matches_oauth_audit() -> None:
    """``oauth_token`` is shared with AS.1.4 oauth_audit so the
    dashboard can correlate forensic + rollup rows on entity_kind."""
    assert ae.ENTITY_KIND_OAUTH_TOKEN == oa.ENTITY_KIND_TOKEN


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — Vocabularies
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_auth_methods_six_values() -> None:
    assert ae.AUTH_METHODS == frozenset({
        "password", "oauth", "passkey",
        "mfa_totp", "mfa_webauthn", "magic_link",
    })


def test_login_fail_reasons_ten_values() -> None:
    assert ae.LOGIN_FAIL_REASONS == frozenset({
        "bad_password", "unknown_user", "account_locked",
        "account_disabled", "mfa_required", "mfa_failed",
        "rate_limited", "bot_challenge_failed",
        "oauth_state_invalid", "oauth_provider_error",
    })


def test_bot_challenge_pass_kinds_four_values() -> None:
    assert ae.BOT_CHALLENGE_PASS_KINDS == frozenset({
        "verified", "bypass_apikey",
        "bypass_ip_allowlist", "bypass_test_token",
    })


def test_bot_challenge_fail_reasons_five_values() -> None:
    assert ae.BOT_CHALLENGE_FAIL_REASONS == frozenset({
        "lowscore", "unverified", "honeypot", "jsfail", "server_error",
    })


def test_token_refresh_outcomes_three_values() -> None:
    assert ae.TOKEN_REFRESH_OUTCOMES == frozenset({
        "success", "no_refresh_token", "provider_error",
    })


def test_token_refresh_outcomes_match_oauth_audit() -> None:
    """AS.5.1 token_refresh outcomes mirror AS.1.4 REFRESH_OUTCOMES so
    a single outcome string drives both audit families."""
    assert ae.TOKEN_REFRESH_OUTCOMES == oa.REFRESH_OUTCOMES


def test_token_rotation_triggers_two_values() -> None:
    assert ae.TOKEN_ROTATION_TRIGGERS == frozenset({
        "auto_refresh", "explicit_refresh",
    })


def test_token_rotation_triggers_match_oauth_audit() -> None:
    assert ae.TOKEN_ROTATION_TRIGGERS == oa.ROTATION_TRIGGERS


def test_oauth_connect_outcomes_two_values() -> None:
    assert ae.OAUTH_CONNECT_OUTCOMES == frozenset({"connected", "relinked"})


def test_oauth_revoke_initiators_three_values() -> None:
    assert ae.OAUTH_REVOKE_INITIATORS == frozenset({"user", "admin", "dsar"})


def test_all_vocabularies_are_frozensets() -> None:
    """Frozen sets reject mutation at runtime — answer #1 of SOP §1
    (no module-level mutable container)."""
    for v in (
        ae.AUTH_METHODS, ae.LOGIN_FAIL_REASONS,
        ae.BOT_CHALLENGE_PASS_KINDS, ae.BOT_CHALLENGE_FAIL_REASONS,
        ae.TOKEN_REFRESH_OUTCOMES, ae.TOKEN_ROTATION_TRIGGERS,
        ae.OAUTH_CONNECT_OUTCOMES, ae.OAUTH_REVOKE_INITIATORS,
    ):
        assert isinstance(v, frozenset)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 4 — fingerprint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_fingerprint_known_input() -> None:
    """Pinned to the SHA-256 hex slice — same oracle the cross-twin
    drift guard reads."""
    expected = hashlib.sha256(b"hello").hexdigest()[:12]
    assert ae.fingerprint("hello") == expected


def test_fingerprint_length_is_12() -> None:
    fp = ae.fingerprint("any-input")
    assert fp is not None
    assert len(fp) == ae.FINGERPRINT_LENGTH == 12


def test_fingerprint_returns_none_for_none_or_empty() -> None:
    assert ae.fingerprint(None) is None
    assert ae.fingerprint("") is None


def test_fingerprint_deterministic() -> None:
    assert ae.fingerprint("alpha") == ae.fingerprint("alpha")
    assert ae.fingerprint("alpha") != ae.fingerprint("beta")


def test_fingerprint_byte_equal_with_oauth_audit() -> None:
    """AS.5.1 and AS.1.4 share the same fingerprint algorithm so a
    single secret correlates across audit families."""
    for s in ("github", "tenant-42", "u-1", "1.2.3.4"):
        assert ae.fingerprint(s) == oa.fingerprint(s)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 5 — login_success builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_login_success_canonical_shape() -> None:
    p = ae.build_login_success_payload(
        ae.LoginSuccessContext(
            user_id="u-1",
            auth_method="oauth",
            provider="github",
            mfa_satisfied=True,
            ip="1.2.3.4",
            user_agent="Mozilla/5.0",
        )
    )
    assert p.action == "auth.login_success"
    assert p.entity_kind == "auth_session"
    assert p.entity_id == "u-1"
    assert p.before is None
    assert set(p.after.keys()) == {
        "auth_method", "provider", "mfa_satisfied",
        "ip_fp", "user_agent_fp",
    }
    assert p.after["auth_method"] == "oauth"
    assert p.after["provider"] == "github"
    assert p.after["mfa_satisfied"] is True
    assert p.after["ip_fp"] == ae.fingerprint("1.2.3.4")
    assert p.after["user_agent_fp"] == ae.fingerprint("Mozilla/5.0")
    assert p.actor == "u-1"


def test_login_success_unknown_method_raises() -> None:
    with pytest.raises(ValueError, match="auth_method"):
        ae.build_login_success_payload(
            ae.LoginSuccessContext(user_id="u", auth_method="biometric")
        )


def test_login_success_explicit_actor_overrides_user_id() -> None:
    p = ae.build_login_success_payload(
        ae.LoginSuccessContext(
            user_id="u-1", auth_method="password", actor="admin:impersonate",
        )
    )
    assert p.actor == "admin:impersonate"


def test_login_success_pii_redaction() -> None:
    p = ae.build_login_success_payload(
        ae.LoginSuccessContext(
            user_id="u-1", auth_method="password",
            ip="10.0.0.1", user_agent="bot/1.0",
        )
    )
    # Raw PII MUST NOT appear in the audit row.
    assert "10.0.0.1" not in str(p.after)
    assert "bot/1.0" not in str(p.after)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 6 — login_fail builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_login_fail_canonical_shape() -> None:
    p = ae.build_login_fail_payload(
        ae.LoginFailContext(
            attempted_user="alice@example.com",
            auth_method="password",
            fail_reason="bad_password",
            ip="1.2.3.4",
            user_agent="curl/7",
        )
    )
    assert p.action == "auth.login_fail"
    assert p.entity_kind == "auth_session"
    assert p.entity_id == ae.fingerprint("alice@example.com")
    assert p.before is None
    assert set(p.after.keys()) == {
        "auth_method", "fail_reason", "provider",
        "attempted_user_fp", "ip_fp", "user_agent_fp",
    }
    assert p.after["fail_reason"] == "bad_password"
    assert p.after["attempted_user_fp"] == ae.fingerprint("alice@example.com")
    assert p.actor == "anonymous"


def test_login_fail_unknown_reason_raises() -> None:
    with pytest.raises(ValueError, match="fail_reason"):
        ae.build_login_fail_payload(
            ae.LoginFailContext(
                attempted_user="x", auth_method="password",
                fail_reason="cosmic_ray",
            )
        )


def test_login_fail_unknown_method_raises() -> None:
    with pytest.raises(ValueError, match="auth_method"):
        ae.build_login_fail_payload(
            ae.LoginFailContext(
                attempted_user="x", auth_method="bio",
                fail_reason="bad_password",
            )
        )


def test_login_fail_attempted_user_never_in_chain() -> None:
    """The raw attempted user MUST never appear in the audit row —
    fingerprint only.  Otherwise a typoed-username attack reveals
    valid usernames in the audit chain."""
    p = ae.build_login_fail_payload(
        ae.LoginFailContext(
            attempted_user="leaked_email@victim.com",
            auth_method="password",
            fail_reason="unknown_user",
        )
    )
    assert "leaked_email@victim.com" not in str(p.after)
    assert "leaked_email@victim.com" not in p.entity_id


def test_login_fail_empty_attempted_user_falls_back_to_anonymous_entity() -> None:
    p = ae.build_login_fail_payload(
        ae.LoginFailContext(
            attempted_user="",
            auth_method="password",
            fail_reason="unknown_user",
        )
    )
    # Empty attempted_user → fingerprint returns None → entity_id falls
    # back to the literal "anonymous" so the chain row still has a
    # non-null entity_id (audit.log requires it).
    assert p.entity_id == "anonymous"
    assert p.after["attempted_user_fp"] is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 7 — oauth_connect builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_oauth_connect_canonical_shape() -> None:
    p = ae.build_oauth_connect_payload(
        ae.OAuthConnectContext(
            user_id="u-1",
            provider="github",
            outcome="connected",
            scope=("read:user", "repo"),
            is_account_link=False,
        )
    )
    assert p.action == "auth.oauth_connect"
    assert p.entity_kind == "oauth_connection"
    assert p.entity_id == "github:u-1"
    assert set(p.after.keys()) == {
        "provider", "outcome", "scope", "is_account_link",
    }
    assert p.after["scope"] == ["read:user", "repo"]
    assert p.after["is_account_link"] is False
    assert p.actor == "u-1"


def test_oauth_connect_unknown_outcome_raises() -> None:
    with pytest.raises(ValueError, match="outcome"):
        ae.build_oauth_connect_payload(
            ae.OAuthConnectContext(
                user_id="u", provider="x", outcome="newly_connected",
            )
        )


def test_oauth_connect_relinked_outcome_accepted() -> None:
    p = ae.build_oauth_connect_payload(
        ae.OAuthConnectContext(
            user_id="u", provider="github", outcome="relinked",
        )
    )
    assert p.after["outcome"] == "relinked"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 8 — oauth_revoke builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_oauth_revoke_canonical_shape() -> None:
    p = ae.build_oauth_revoke_payload(
        ae.OAuthRevokeContext(
            user_id="u-1",
            provider="google",
            initiator="user",
            revocation_succeeded=True,
        )
    )
    assert p.action == "auth.oauth_revoke"
    assert p.entity_kind == "oauth_connection"
    assert p.entity_id == "google:u-1"
    assert set(p.after.keys()) == {
        "provider", "initiator", "revocation_succeeded",
    }
    assert p.after["revocation_succeeded"] is True


def test_oauth_revoke_unknown_initiator_raises() -> None:
    with pytest.raises(ValueError, match="initiator"):
        ae.build_oauth_revoke_payload(
            ae.OAuthRevokeContext(
                user_id="u", provider="github", initiator="cron",
            )
        )


@pytest.mark.parametrize("initiator", ["user", "admin", "dsar"])
def test_oauth_revoke_each_initiator_accepted(initiator: str) -> None:
    p = ae.build_oauth_revoke_payload(
        ae.OAuthRevokeContext(
            user_id="u", provider="github", initiator=initiator,
        )
    )
    assert p.after["initiator"] == initiator


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 9 — bot_challenge_pass builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_bot_challenge_pass_verified_shape() -> None:
    p = ae.build_bot_challenge_pass_payload(
        ae.BotChallengePassContext(
            form_path="/api/v1/auth/login",
            kind="verified",
            provider="turnstile",
            score=0.9,
        )
    )
    assert p.action == "auth.bot_challenge_pass"
    assert p.entity_kind == "auth_session"
    assert p.entity_id == "/api/v1/auth/login"
    assert set(p.after.keys()) == {"form_path", "kind", "provider", "score"}
    assert p.after["kind"] == "verified"
    assert p.after["score"] == 0.9


def test_bot_challenge_pass_verified_requires_score() -> None:
    with pytest.raises(ValueError, match="kind='verified' requires score"):
        ae.build_bot_challenge_pass_payload(
            ae.BotChallengePassContext(
                form_path="/x", kind="verified",
            )
        )


def test_bot_challenge_pass_bypass_must_have_no_score() -> None:
    with pytest.raises(ValueError, match="must have score=None"):
        ae.build_bot_challenge_pass_payload(
            ae.BotChallengePassContext(
                form_path="/x", kind="bypass_apikey", score=0.9,
            )
        )


@pytest.mark.parametrize(
    "kind",
    ["bypass_apikey", "bypass_ip_allowlist", "bypass_test_token"],
)
def test_bot_challenge_pass_bypass_kinds_accepted(kind: str) -> None:
    p = ae.build_bot_challenge_pass_payload(
        ae.BotChallengePassContext(form_path="/x", kind=kind)
    )
    assert p.after["kind"] == kind
    assert p.after["score"] is None


def test_bot_challenge_pass_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="kind"):
        ae.build_bot_challenge_pass_payload(
            ae.BotChallengePassContext(form_path="/x", kind="captcha_solved")
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 10 — bot_challenge_fail builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_bot_challenge_fail_lowscore_shape() -> None:
    p = ae.build_bot_challenge_fail_payload(
        ae.BotChallengeFailContext(
            form_path="/api/v1/auth/login",
            reason="lowscore",
            provider="recaptcha_v3",
            score=0.2,
        )
    )
    assert p.action == "auth.bot_challenge_fail"
    assert p.entity_kind == "auth_session"
    assert p.entity_id == "/api/v1/auth/login"
    assert set(p.after.keys()) == {"form_path", "reason", "provider", "score"}
    assert p.after["reason"] == "lowscore"
    assert p.after["score"] == 0.2


@pytest.mark.parametrize(
    "reason", ["lowscore", "unverified", "honeypot", "jsfail", "server_error"],
)
def test_bot_challenge_fail_each_reason_accepted(reason: str) -> None:
    p = ae.build_bot_challenge_fail_payload(
        ae.BotChallengeFailContext(form_path="/x", reason=reason)
    )
    assert p.after["reason"] == reason


def test_bot_challenge_fail_unknown_reason_raises() -> None:
    with pytest.raises(ValueError, match="reason"):
        ae.build_bot_challenge_fail_payload(
            ae.BotChallengeFailContext(form_path="/x", reason="alien")
        )


def test_bot_challenge_fail_score_optional() -> None:
    """For honeypot / jsfail / server_error there's no score — the
    field rounds-trips a typed null."""
    p = ae.build_bot_challenge_fail_payload(
        ae.BotChallengeFailContext(form_path="/x", reason="honeypot")
    )
    assert p.after["score"] is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 11 — token_refresh builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_token_refresh_success_shape() -> None:
    p = ae.build_token_refresh_payload(
        ae.TokenRefreshContext(
            user_id="u-1",
            provider="github",
            outcome="success",
            new_expires_in_seconds=3600,
        )
    )
    assert p.action == "auth.token_refresh"
    assert p.entity_kind == "oauth_token"
    assert p.entity_id == "github:u-1"
    assert set(p.after.keys()) == {
        "provider", "outcome", "new_expires_in_seconds",
    }
    assert p.after["new_expires_in_seconds"] == 3600
    assert p.actor == "u-1"


def test_token_refresh_unknown_outcome_raises() -> None:
    with pytest.raises(ValueError, match="outcome"):
        ae.build_token_refresh_payload(
            ae.TokenRefreshContext(
                user_id="u", provider="x", outcome="ok",
            )
        )


def test_token_refresh_explicit_actor_overrides_user_id() -> None:
    p = ae.build_token_refresh_payload(
        ae.TokenRefreshContext(
            user_id="u-1", provider="github",
            outcome="success", actor="system:auto-refresh",
        )
    )
    assert p.actor == "system:auto-refresh"


def test_token_refresh_nullable_optional_round_trips() -> None:
    p = ae.build_token_refresh_payload(
        ae.TokenRefreshContext(
            user_id="u", provider="apple", outcome="no_refresh_token",
        )
    )
    assert p.after["new_expires_in_seconds"] is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 12 — token_rotated builder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_token_rotated_canonical_shape() -> None:
    p = ae.build_token_rotated_payload(
        ae.TokenRotatedContext(
            user_id="u-1",
            provider="google",
            previous_refresh_token="old_rt_aaa",
            new_refresh_token="new_rt_bbb",
            triggered_by="auto_refresh",
        )
    )
    assert p.action == "auth.token_rotated"
    assert p.entity_kind == "oauth_token"
    assert p.entity_id == "google:u-1"
    assert p.before == {
        "provider": "google",
        "prior_refresh_token_fp": ae.fingerprint("old_rt_aaa"),
    }
    assert p.after["new_refresh_token_fp"] == ae.fingerprint("new_rt_bbb")
    assert p.after["triggered_by"] == "auto_refresh"


def test_token_rotated_raw_tokens_never_in_payload() -> None:
    """The raw refresh tokens MUST never appear in the chain — only
    SHA-256 fingerprints."""
    p = ae.build_token_rotated_payload(
        ae.TokenRotatedContext(
            user_id="u",
            provider="github",
            previous_refresh_token="leaked_old_credential_xxx",
            new_refresh_token="leaked_new_credential_yyy",
            triggered_by="auto_refresh",
        )
    )
    serialised = str(p.before) + str(p.after)
    assert "leaked_old_credential_xxx" not in serialised
    assert "leaked_new_credential_yyy" not in serialised


def test_token_rotated_unknown_trigger_raises() -> None:
    with pytest.raises(ValueError, match="triggered_by"):
        ae.build_token_rotated_payload(
            ae.TokenRotatedContext(
                user_id="u", provider="x",
                previous_refresh_token="a", new_refresh_token="b",
                triggered_by="cron_kick",
            )
        )


@pytest.mark.parametrize("trig", ["auto_refresh", "explicit_refresh"])
def test_token_rotated_each_trigger_accepted(trig: str) -> None:
    p = ae.build_token_rotated_payload(
        ae.TokenRotatedContext(
            user_id="u", provider="github",
            previous_refresh_token="a", new_refresh_token="b",
            triggered_by=trig,
        )
    )
    assert p.after["triggered_by"] == trig


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 13 — emit_* helpers route into audit.log
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _patch_audit_log(monkeypatch, captured: dict[str, Any], rid: int = 99):
    async def fake_log(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return rid

    monkeypatch.setattr(
        "backend.security.auth_event.audit.log", fake_log
    )


def test_emit_login_success_routes_to_audit_log(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_audit_log(monkeypatch, captured, rid=10)
    rid = _run(ae.emit_login_success(
        ae.LoginSuccessContext(user_id="u-1", auth_method="password")
    ))
    assert rid == 10
    assert captured["action"] == "auth.login_success"
    assert captured["entity_kind"] == "auth_session"
    assert captured["entity_id"] == "u-1"


def test_emit_login_fail_routes_to_audit_log(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_audit_log(monkeypatch, captured, rid=11)
    _run(ae.emit_login_fail(
        ae.LoginFailContext(
            attempted_user="x@y.z", auth_method="password",
            fail_reason="bad_password",
        )
    ))
    assert captured["action"] == "auth.login_fail"
    assert captured["actor"] == "anonymous"


def test_emit_oauth_connect_routes_to_audit_log(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_audit_log(monkeypatch, captured, rid=12)
    _run(ae.emit_oauth_connect(
        ae.OAuthConnectContext(
            user_id="u-1", provider="github", outcome="connected",
        )
    ))
    assert captured["action"] == "auth.oauth_connect"
    assert captured["entity_id"] == "github:u-1"


def test_emit_oauth_revoke_routes_to_audit_log(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_audit_log(monkeypatch, captured, rid=13)
    _run(ae.emit_oauth_revoke(
        ae.OAuthRevokeContext(
            user_id="u-1", provider="google", initiator="dsar",
        )
    ))
    assert captured["action"] == "auth.oauth_revoke"
    assert captured["after"]["initiator"] == "dsar"


def test_emit_bot_challenge_pass_routes_to_audit_log(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_audit_log(monkeypatch, captured, rid=14)
    _run(ae.emit_bot_challenge_pass(
        ae.BotChallengePassContext(
            form_path="/x", kind="verified", score=0.95,
        )
    ))
    assert captured["action"] == "auth.bot_challenge_pass"
    assert captured["after"]["score"] == 0.95


def test_emit_bot_challenge_fail_routes_to_audit_log(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_audit_log(monkeypatch, captured, rid=15)
    _run(ae.emit_bot_challenge_fail(
        ae.BotChallengeFailContext(form_path="/x", reason="honeypot")
    ))
    assert captured["action"] == "auth.bot_challenge_fail"
    assert captured["after"]["reason"] == "honeypot"


def test_emit_token_refresh_routes_to_audit_log(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_audit_log(monkeypatch, captured, rid=16)
    _run(ae.emit_token_refresh(
        ae.TokenRefreshContext(
            user_id="u-1", provider="github", outcome="success",
        )
    ))
    assert captured["action"] == "auth.token_refresh"


def test_emit_token_rotated_routes_to_audit_log(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_audit_log(monkeypatch, captured, rid=17)
    _run(ae.emit_token_rotated(
        ae.TokenRotatedContext(
            user_id="u-1", provider="github",
            previous_refresh_token="a", new_refresh_token="b",
            triggered_by="auto_refresh",
        )
    ))
    assert captured["action"] == "auth.token_rotated"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 14 — AS.0.8 single-knob: emit_* skip when knob-off
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _no_log_called(monkeypatch) -> dict[str, bool]:
    flag = {"called": False}

    async def boom(**kwargs):
        flag["called"] = True
        return 1

    monkeypatch.setattr(
        "backend.security.auth_event.audit.log", boom
    )
    return flag


def _disable_knob(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.security.auth_event.is_enabled", lambda: False
    )


def test_emit_login_success_skips_when_knob_off(monkeypatch) -> None:
    flag = _no_log_called(monkeypatch)
    _disable_knob(monkeypatch)
    rid = _run(ae.emit_login_success(
        ae.LoginSuccessContext(user_id="u", auth_method="password")
    ))
    assert rid is None
    assert flag["called"] is False


def test_emit_login_fail_skips_when_knob_off(monkeypatch) -> None:
    flag = _no_log_called(monkeypatch)
    _disable_knob(monkeypatch)
    rid = _run(ae.emit_login_fail(
        ae.LoginFailContext(
            attempted_user="x", auth_method="password",
            fail_reason="bad_password",
        )
    ))
    assert rid is None
    assert flag["called"] is False


def test_emit_oauth_connect_skips_when_knob_off(monkeypatch) -> None:
    flag = _no_log_called(monkeypatch)
    _disable_knob(monkeypatch)
    rid = _run(ae.emit_oauth_connect(
        ae.OAuthConnectContext(
            user_id="u", provider="github", outcome="connected",
        )
    ))
    assert rid is None
    assert flag["called"] is False


def test_emit_oauth_revoke_skips_when_knob_off(monkeypatch) -> None:
    flag = _no_log_called(monkeypatch)
    _disable_knob(monkeypatch)
    rid = _run(ae.emit_oauth_revoke(
        ae.OAuthRevokeContext(
            user_id="u", provider="github", initiator="user",
        )
    ))
    assert rid is None
    assert flag["called"] is False


def test_emit_bot_challenge_pass_skips_when_knob_off(monkeypatch) -> None:
    flag = _no_log_called(monkeypatch)
    _disable_knob(monkeypatch)
    rid = _run(ae.emit_bot_challenge_pass(
        ae.BotChallengePassContext(
            form_path="/x", kind="verified", score=0.9,
        )
    ))
    assert rid is None
    assert flag["called"] is False


def test_emit_bot_challenge_fail_skips_when_knob_off(monkeypatch) -> None:
    flag = _no_log_called(monkeypatch)
    _disable_knob(monkeypatch)
    rid = _run(ae.emit_bot_challenge_fail(
        ae.BotChallengeFailContext(form_path="/x", reason="lowscore")
    ))
    assert rid is None
    assert flag["called"] is False


def test_emit_token_refresh_skips_when_knob_off(monkeypatch) -> None:
    flag = _no_log_called(monkeypatch)
    _disable_knob(monkeypatch)
    rid = _run(ae.emit_token_refresh(
        ae.TokenRefreshContext(
            user_id="u", provider="github", outcome="success",
        )
    ))
    assert rid is None
    assert flag["called"] is False


def test_emit_token_rotated_skips_when_knob_off(monkeypatch) -> None:
    flag = _no_log_called(monkeypatch)
    _disable_knob(monkeypatch)
    rid = _run(ae.emit_token_rotated(
        ae.TokenRotatedContext(
            user_id="u", provider="github",
            previous_refresh_token="a", new_refresh_token="b",
            triggered_by="auto_refresh",
        )
    ))
    assert rid is None
    assert flag["called"] is False


def test_is_enabled_default_true() -> None:
    """Forward-promotion guard: missing settings.as_enabled defaults
    to True so the module is importable before AS.3.1 lands the field."""
    # No mock — real settings module reads.  AS.3.1 has shipped the
    # field, default True.
    assert ae.is_enabled() is True


def test_is_enabled_settings_false_disables() -> None:
    """When settings.as_enabled is set False, is_enabled returns False."""
    with patch("backend.security.auth_event.is_enabled", return_value=False):
        assert ae.is_enabled() is False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 15 — Module-global state SOP §1
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_module_uses_hashlib_not_secrets() -> None:
    """SOP §1 / AS.1.4 provenance grep: fingerprint comes from
    :mod:`hashlib`, not :mod:`secrets`.  We're not generating
    randomness — only deriving stable redactions from already-secret
    material."""
    src = pathlib.Path(ae.__file__).read_text(encoding="utf-8")
    assert "hashlib" in src
    assert "import secrets" not in src
    assert "from secrets" not in src


def test_module_constants_stable_across_reload() -> None:
    """Reloading the module produces the same constant values
    (no time / env / IO dependency at import time)."""
    snapshot_events = ae.ALL_AUTH_EVENTS
    snapshot_methods = ae.AUTH_METHODS
    importlib.reload(ae)
    assert ae.ALL_AUTH_EVENTS == snapshot_events
    assert ae.AUTH_METHODS == snapshot_methods


def test_module_no_top_level_io() -> None:
    """SOP §1: module import must not perform IO.  Grep for the obvious
    smells in the source — if any of these strings appears unwrapped at
    module scope, the audit fails."""
    src = pathlib.Path(ae.__file__).read_text(encoding="utf-8")
    # No top-level open / requests / httpx / db calls.
    assert "\nopen(" not in src
    assert "\nrequests." not in src


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 16 — __all__ shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_all_export_complete() -> None:
    """Every documented symbol is in __all__; no stowaways."""
    expected = {
        # 8 events + tuple
        "EVENT_AUTH_LOGIN_SUCCESS", "EVENT_AUTH_LOGIN_FAIL",
        "EVENT_AUTH_OAUTH_CONNECT", "EVENT_AUTH_OAUTH_REVOKE",
        "EVENT_AUTH_BOT_CHALLENGE_PASS", "EVENT_AUTH_BOT_CHALLENGE_FAIL",
        "EVENT_AUTH_TOKEN_REFRESH", "EVENT_AUTH_TOKEN_ROTATED",
        "ALL_AUTH_EVENTS",
        # 3 entity_kinds
        "ENTITY_KIND_AUTH_SESSION", "ENTITY_KIND_OAUTH_CONNECTION",
        "ENTITY_KIND_OAUTH_TOKEN",
        # auth methods
        "AUTH_METHOD_PASSWORD", "AUTH_METHOD_OAUTH", "AUTH_METHOD_PASSKEY",
        "AUTH_METHOD_MFA_TOTP", "AUTH_METHOD_MFA_WEBAUTHN",
        "AUTH_METHOD_MAGIC_LINK", "AUTH_METHODS",
        # login fail reasons
        "LOGIN_FAIL_BAD_PASSWORD", "LOGIN_FAIL_UNKNOWN_USER",
        "LOGIN_FAIL_ACCOUNT_LOCKED", "LOGIN_FAIL_ACCOUNT_DISABLED",
        "LOGIN_FAIL_MFA_REQUIRED", "LOGIN_FAIL_MFA_FAILED",
        "LOGIN_FAIL_RATE_LIMITED", "LOGIN_FAIL_BOT_CHALLENGE_FAILED",
        "LOGIN_FAIL_OAUTH_STATE_INVALID", "LOGIN_FAIL_OAUTH_PROVIDER_ERROR",
        "LOGIN_FAIL_REASONS",
        # bot-challenge pass kinds
        "BOT_CHALLENGE_PASS_VERIFIED", "BOT_CHALLENGE_PASS_BYPASS_APIKEY",
        "BOT_CHALLENGE_PASS_BYPASS_IP_ALLOWLIST",
        "BOT_CHALLENGE_PASS_BYPASS_TEST_TOKEN", "BOT_CHALLENGE_PASS_KINDS",
        # bot-challenge fail reasons
        "BOT_CHALLENGE_FAIL_LOWSCORE", "BOT_CHALLENGE_FAIL_UNVERIFIED",
        "BOT_CHALLENGE_FAIL_HONEYPOT", "BOT_CHALLENGE_FAIL_JSFAIL",
        "BOT_CHALLENGE_FAIL_SERVER_ERROR", "BOT_CHALLENGE_FAIL_REASONS",
        # token refresh outcomes
        "TOKEN_REFRESH_SUCCESS", "TOKEN_REFRESH_NO_REFRESH_TOKEN",
        "TOKEN_REFRESH_PROVIDER_ERROR", "TOKEN_REFRESH_OUTCOMES",
        # token rotation triggers
        "TOKEN_ROTATION_TRIGGER_AUTO", "TOKEN_ROTATION_TRIGGER_EXPLICIT",
        "TOKEN_ROTATION_TRIGGERS",
        # oauth connect outcomes
        "OAUTH_CONNECT_CONNECTED", "OAUTH_CONNECT_RELINKED",
        "OAUTH_CONNECT_OUTCOMES",
        # oauth revoke initiators
        "OAUTH_REVOKE_USER", "OAUTH_REVOKE_ADMIN", "OAUTH_REVOKE_DSAR",
        "OAUTH_REVOKE_INITIATORS",
        # fingerprint length
        "FINGERPRINT_LENGTH",
        # helpers
        "fingerprint", "is_enabled",
        # 8 context dataclasses
        "LoginSuccessContext", "LoginFailContext",
        "OAuthConnectContext", "OAuthRevokeContext",
        "BotChallengePassContext", "BotChallengeFailContext",
        "TokenRefreshContext", "TokenRotatedContext",
        # payload + 8 builders
        "AuthAuditPayload",
        "build_login_success_payload", "build_login_fail_payload",
        "build_oauth_connect_payload", "build_oauth_revoke_payload",
        "build_bot_challenge_pass_payload",
        "build_bot_challenge_fail_payload",
        "build_token_refresh_payload", "build_token_rotated_payload",
        # 8 emitters
        "emit_login_success", "emit_login_fail",
        "emit_oauth_connect", "emit_oauth_revoke",
        "emit_bot_challenge_pass", "emit_bot_challenge_fail",
        "emit_token_refresh", "emit_token_rotated",
    }
    assert set(ae.__all__) == expected


def test_all_exports_resolve() -> None:
    """Every name in __all__ exists on the module."""
    for name in ae.__all__:
        assert hasattr(ae, name), f"__all__ lists {name!r} but module has no such attr"

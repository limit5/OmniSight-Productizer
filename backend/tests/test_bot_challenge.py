"""AS.3.1 — `backend.security.bot_challenge` contract tests.

Validates the unified bot-challenge interface defined in
:mod:`backend.security.bot_challenge`.  Four provider verifiers
(Turnstile, reCAPTCHA v2 / v3, hCaptcha) plus the bypass-axis
precedence (AS.0.6 §4) plus the phase-aware classifier (AS.0.5 §2)
plus 19 audit-event canonical strings (AS.0.5 §3 + AS.0.6 §3) plus
the AS.0.8 single-knob short-circuit.

Test families
─────────────
1.  Constants & event vocabulary (AS.0.5 §3 + AS.0.6 §3 byte-equality)
2.  Provider enum + secret-env routing
3.  Bypass evaluation (axis precedence A → C → B → path)
4.  Path-prefix bypass list (AS.0.5 §8.1 inventory drift guard)
5.  IP-allowlist matching (CIDR, IPv4 + IPv6, corrupt entry skip)
6.  Test-token header (constant-time compare, ≥32 char invariant)
7.  Provider-response parsing (score normalisation per provider)
8.  Phase-aware classify_outcome (fail-open / fail-closed matrix)
9.  Top-level verify() orchestrator (knob → bypass → verify → classify)
10. Provider verifier HTTP integration (mocked transport, 4 providers)
11. Knob-off short-circuit (AS.0.8 single-knob)
12. Module-global state audit (per SOP §1)
13. AS.0.5 §8 drift guards (bypass-list + provider-secret-env + outcome map)
14. Action-mismatch demotion (anti-replay across forms)
15. Server-error fail-open invariant (Phase 3 too)
"""

from __future__ import annotations

import importlib
import pathlib
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from backend.security import bot_challenge as bc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — Constants & event vocabulary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_event_strings_match_as_0_5_section_3_byte_equality():
    """AS.0.5 §3 invariant: 13 ``bot_challenge.*`` strings + 4 phase
    strings must be byte-equal across plan / module / TS twin (AS.3.2)."""
    expected = {
        "bot_challenge.pass": bc.EVENT_BOT_CHALLENGE_PASS,
        "bot_challenge.unverified_lowscore": bc.EVENT_BOT_CHALLENGE_UNVERIFIED_LOWSCORE,
        "bot_challenge.unverified_servererr": bc.EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR,
        "bot_challenge.blocked_lowscore": bc.EVENT_BOT_CHALLENGE_BLOCKED_LOWSCORE,
        "bot_challenge.jsfail_fallback_recaptcha": bc.EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_RECAPTCHA,
        "bot_challenge.jsfail_fallback_hcaptcha": bc.EVENT_BOT_CHALLENGE_JSFAIL_FALLBACK_HCAPTCHA,
        "bot_challenge.jsfail_honeypot_pass": bc.EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_PASS,
        "bot_challenge.jsfail_honeypot_fail": bc.EVENT_BOT_CHALLENGE_JSFAIL_HONEYPOT_FAIL,
        "bot_challenge.bypass_apikey": bc.EVENT_BOT_CHALLENGE_BYPASS_APIKEY,
        "bot_challenge.bypass_webhook": bc.EVENT_BOT_CHALLENGE_BYPASS_WEBHOOK,
        "bot_challenge.bypass_chatops": bc.EVENT_BOT_CHALLENGE_BYPASS_CHATOPS,
        "bot_challenge.bypass_bootstrap": bc.EVENT_BOT_CHALLENGE_BYPASS_BOOTSTRAP,
        "bot_challenge.bypass_probe": bc.EVENT_BOT_CHALLENGE_BYPASS_PROBE,
        "bot_challenge.bypass_ip_allowlist": bc.EVENT_BOT_CHALLENGE_BYPASS_IP_ALLOWLIST,
        "bot_challenge.bypass_test_token": bc.EVENT_BOT_CHALLENGE_BYPASS_TEST_TOKEN,
        "bot_challenge.phase_advance_p1_to_p2": bc.EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P1_TO_P2,
        "bot_challenge.phase_advance_p2_to_p3": bc.EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P2_TO_P3,
        "bot_challenge.phase_revert_p3_to_p2": bc.EVENT_BOT_CHALLENGE_PHASE_REVERT_P3_TO_P2,
        "bot_challenge.phase_revert_p2_to_p1": bc.EVENT_BOT_CHALLENGE_PHASE_REVERT_P2_TO_P1,
    }
    for spec_string, mod_constant in expected.items():
        assert spec_string == mod_constant


def test_all_bot_challenge_events_count_is_19():
    """8 verify outcomes + 7 bypass + 4 phase = 19 events."""
    assert len(bc.ALL_BOT_CHALLENGE_EVENTS) == 19
    assert len(set(bc.ALL_BOT_CHALLENGE_EVENTS)) == 19  # no dupes


def test_all_outcomes_count_is_15():
    """4 verify outcomes + 7 bypass + 4 jsfail = 15 outcomes."""
    assert len(bc.ALL_OUTCOMES) == 15
    assert len(set(bc.ALL_OUTCOMES)) == 15


def test_event_for_outcome_covers_every_outcome():
    """Every outcome literal MUST map to exactly one event string."""
    for outcome in bc.ALL_OUTCOMES:
        event = bc.event_for_outcome(outcome)
        assert event in bc.ALL_BOT_CHALLENGE_EVENTS


def test_event_for_outcome_rejects_unknown():
    with pytest.raises(ValueError):
        bc.event_for_outcome("not_a_real_outcome")


def test_test_token_header_constant():
    """AS.0.6 §2.3: header name is fixed ``X-OmniSight-Test-Token``."""
    assert bc.TEST_TOKEN_HEADER == "X-OmniSight-Test-Token"


def test_default_score_threshold_is_half():
    """AS.0.5 §2.4 + design doc §3.5 — threshold pinned at 0.5."""
    assert bc.DEFAULT_SCORE_THRESHOLD == 0.5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — Provider enum + secret-env routing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_provider_enum_has_four_members():
    """Turnstile / reCAPTCHA v2 / reCAPTCHA v3 / hCaptcha — four exact."""
    assert {p.value for p in bc.Provider} == {
        "turnstile", "recaptcha_v2", "recaptcha_v3", "hcaptcha"
    }


def test_secret_env_for_turnstile():
    assert bc.secret_env_for(bc.Provider.TURNSTILE) == "OMNISIGHT_TURNSTILE_SECRET"


def test_secret_env_for_recaptcha_v2_and_v3_share_env():
    """v2 + v3 share OMNISIGHT_RECAPTCHA_SECRET (same Google account
    project)."""
    assert bc.secret_env_for(bc.Provider.RECAPTCHA_V2) == "OMNISIGHT_RECAPTCHA_SECRET"
    assert bc.secret_env_for(bc.Provider.RECAPTCHA_V3) == "OMNISIGHT_RECAPTCHA_SECRET"


def test_secret_env_for_hcaptcha():
    assert bc.secret_env_for(bc.Provider.HCAPTCHA) == "OMNISIGHT_HCAPTCHA_SECRET"


def test_siteverify_urls_are_pinned():
    """AS.0.5 §5 invariant — endpoint URLs are part of the contract."""
    assert bc.SITEVERIFY_URLS[bc.Provider.TURNSTILE] == \
        "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    assert bc.SITEVERIFY_URLS[bc.Provider.RECAPTCHA_V2] == \
        "https://www.google.com/recaptcha/api/siteverify"
    assert bc.SITEVERIFY_URLS[bc.Provider.RECAPTCHA_V3] == \
        "https://www.google.com/recaptcha/api/siteverify"
    assert bc.SITEVERIFY_URLS[bc.Provider.HCAPTCHA] == \
        "https://hcaptcha.com/siteverify"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — Bypass evaluation (axis precedence)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_evaluate_bypass_returns_none_when_nothing_matches():
    ctx = bc.BypassContext()
    assert bc.evaluate_bypass(ctx) is None


def test_evaluate_bypass_apikey_match():
    ctx = bc.BypassContext(
        caller_kind="apikey_omni",
        api_key_id="ak-9",
        api_key_prefix="omni_xyz",
        widget_action="login",
    )
    reason = bc.evaluate_bypass(ctx)
    assert reason is not None
    assert reason.outcome == bc.OUTCOME_BYPASS_APIKEY
    assert reason.audit_metadata["caller_kind"] == "apikey_omni"
    assert reason.audit_metadata["key_id"] == "ak-9"
    assert reason.audit_metadata["key_prefix"] == "omni_xyz"


def test_evaluate_bypass_unknown_caller_kind_does_not_bypass():
    ctx = bc.BypassContext(caller_kind="random_caller_we_dont_know")
    assert bc.evaluate_bypass(ctx) is None


def test_evaluate_bypass_test_token_match():
    expected = "x" * 32
    ctx = bc.BypassContext(
        test_token_header_value=expected,
        test_token_expected=expected,
        tenant_id="t-1",
    )
    reason = bc.evaluate_bypass(ctx)
    assert reason is not None
    assert reason.outcome == bc.OUTCOME_BYPASS_TEST_TOKEN
    assert reason.audit_metadata["tenant_id_or_null"] == "t-1"
    # token_fp is last-12 SHA-256 hex
    assert len(reason.audit_metadata["token_fp"]) == 12


def test_evaluate_bypass_test_token_short_envvar_does_not_bypass():
    """AS.0.6 §2.3 — env < 32 chars treated as unset (fail-closed on
    bypass axis)."""
    short = "abc"
    ctx = bc.BypassContext(
        test_token_header_value=short, test_token_expected=short,
    )
    assert bc.evaluate_bypass(ctx) is None


def test_evaluate_bypass_test_token_mismatched_does_not_bypass():
    expected = "x" * 32
    ctx = bc.BypassContext(
        test_token_header_value="y" * 32,
        test_token_expected=expected,
    )
    assert bc.evaluate_bypass(ctx) is None


def test_evaluate_bypass_ip_allowlist_match():
    ctx = bc.BypassContext(
        client_ip="192.0.2.42",
        tenant_ip_allowlist=("192.0.2.0/24",),
    )
    reason = bc.evaluate_bypass(ctx)
    assert reason is not None
    assert reason.outcome == bc.OUTCOME_BYPASS_IP_ALLOWLIST
    assert reason.audit_metadata["cidr_match"] == "192.0.2.0/24"
    assert reason.audit_metadata["client_ip_subnet"] == "192.0.2.0/24"
    # /24 is the upper bound of "narrow" — so it's wide
    assert reason.audit_metadata["wide_cidr"] is True


def test_evaluate_bypass_ip_allowlist_narrow_cidr_not_wide():
    ctx = bc.BypassContext(
        client_ip="192.0.2.42",
        tenant_ip_allowlist=("192.0.2.42/32",),
    )
    reason = bc.evaluate_bypass(ctx)
    assert reason is not None
    assert reason.audit_metadata["wide_cidr"] is False


def test_evaluate_bypass_ipv6_allowlist():
    ctx = bc.BypassContext(
        client_ip="2001:db8:abcd::1",
        tenant_ip_allowlist=("2001:db8:abcd::/48",),
    )
    reason = bc.evaluate_bypass(ctx)
    assert reason is not None
    assert reason.outcome == bc.OUTCOME_BYPASS_IP_ALLOWLIST


def test_evaluate_bypass_corrupt_allowlist_entry_skipped():
    ctx = bc.BypassContext(
        client_ip="192.0.2.42",
        tenant_ip_allowlist=("not-a-cidr", "192.0.2.0/24"),
    )
    reason = bc.evaluate_bypass(ctx)
    assert reason is not None  # second entry matched


def test_evaluate_bypass_path_prefix_match():
    ctx = bc.BypassContext(path="/api/v1/livez")
    reason = bc.evaluate_bypass(ctx)
    assert reason is not None
    assert reason.outcome == bc.OUTCOME_BYPASS_PROBE


def test_evaluate_bypass_path_bootstrap():
    ctx = bc.BypassContext(path="/api/v1/bootstrap/init-tenant")
    reason = bc.evaluate_bypass(ctx)
    assert reason is not None
    assert reason.outcome == bc.OUTCOME_BYPASS_BOOTSTRAP


def test_evaluate_bypass_path_chatops_routes_correctly():
    """``/api/v1/chatops/webhook/`` must route to chatops, not webhook."""
    ctx = bc.BypassContext(path="/api/v1/chatops/webhook/slack")
    reason = bc.evaluate_bypass(ctx)
    assert reason is not None
    assert reason.outcome == bc.OUTCOME_BYPASS_CHATOPS


def test_evaluate_bypass_precedence_apikey_wins_over_ip():
    """AS.0.6 §4: A (apikey) > B (ip_allowlist)."""
    ctx = bc.BypassContext(
        caller_kind="apikey_omni",
        api_key_id="ak-1",
        client_ip="192.0.2.42",
        tenant_ip_allowlist=("192.0.2.0/24",),
    )
    reason = bc.evaluate_bypass(ctx)
    assert reason is not None
    assert reason.outcome == bc.OUTCOME_BYPASS_APIKEY
    assert "ip_allowlist" in reason.also_matched


def test_evaluate_bypass_precedence_apikey_wins_over_test_token():
    """AS.0.6 §4: A (apikey) > C (test_token)."""
    expected = "x" * 32
    ctx = bc.BypassContext(
        caller_kind="apikey_omni",
        api_key_id="ak-1",
        test_token_header_value=expected,
        test_token_expected=expected,
    )
    reason = bc.evaluate_bypass(ctx)
    assert reason.outcome == bc.OUTCOME_BYPASS_APIKEY
    assert "test_token" in reason.also_matched


def test_evaluate_bypass_precedence_test_token_wins_over_ip():
    """AS.0.6 §4: C (test_token) > B (ip_allowlist)."""
    expected = "x" * 32
    ctx = bc.BypassContext(
        test_token_header_value=expected,
        test_token_expected=expected,
        client_ip="192.0.2.42",
        tenant_ip_allowlist=("192.0.2.0/24",),
    )
    reason = bc.evaluate_bypass(ctx)
    assert reason.outcome == bc.OUTCOME_BYPASS_TEST_TOKEN
    assert "ip_allowlist" in reason.also_matched


def test_evaluate_bypass_precedence_full_three_axis_match():
    """All three identity axes hit + path: apikey wins, all three
    others land in also_matched."""
    expected = "x" * 32
    ctx = bc.BypassContext(
        caller_kind="apikey_omni",
        api_key_id="ak-1",
        test_token_header_value=expected,
        test_token_expected=expected,
        client_ip="192.0.2.42",
        tenant_ip_allowlist=("192.0.2.0/24",),
        path="/api/v1/livez",
    )
    reason = bc.evaluate_bypass(ctx)
    assert reason.outcome == bc.OUTCOME_BYPASS_APIKEY
    assert set(reason.also_matched) == {"test_token", "ip_allowlist", "path"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 4 — Path-prefix bypass list inventory drift guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_as_0_5_bypass_list_aligned_with_inventory():
    """AS.0.5 §8.1 invariant: bot_challenge bypass list 必對齊 AS.0.1
    §4.5 inventory."""
    expected_paths = {
        "/api/v1/livez",
        "/api/v1/readyz",
        "/api/v1/healthz",
        "/api/v1/bootstrap/",
        "/api/v1/webhooks/",
        "/api/v1/chatops/webhook/",
        "/api/v1/auth/oidc/",
        "/api/v1/auth/mfa/challenge",
        "/api/v1/auth/mfa/webauthn/challenge/",
    }
    expected_kinds = {"apikey_omni", "apikey_legacy", "metrics_token"}
    assert expected_paths.issubset(bc._BYPASS_PATH_PREFIXES), (
        f"AS.0.5 §8.1 / AS.0.1 §4.5 bypass list drift: missing "
        f"{expected_paths - bc._BYPASS_PATH_PREFIXES}"
    )
    assert expected_kinds.issubset(bc._BYPASS_CALLER_KINDS), (
        f"AS.0.5 §8.1 / AS.0.6 caller kinds drift: missing "
        f"{expected_kinds - bc._BYPASS_CALLER_KINDS}"
    )


def test_as_0_5_provider_site_secret_envs_distinct():
    """AS.0.5 §8.3 invariant: 三 provider site secret 各自獨立 env，
    禁共用."""
    expected = {
        "turnstile": "OMNISIGHT_TURNSTILE_SECRET",
        "recaptcha": "OMNISIGHT_RECAPTCHA_SECRET",
        "hcaptcha": "OMNISIGHT_HCAPTCHA_SECRET",
    }
    assert dict(bc._PROVIDER_SECRET_ENVS) == expected, (
        f"AS.0.5 §8.3 provider env drift: {dict(bc._PROVIDER_SECRET_ENVS)}"
    )


def test_login_path_is_NOT_bypassed():
    """AS.0.5 §8.1 note: ``/api/v1/auth/login`` must NOT be bypassed —
    Turnstile's primary protection target."""
    ctx = bc.BypassContext(path="/api/v1/auth/login")
    assert bc.evaluate_bypass(ctx) is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 5 — IP-allowlist matching
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_ip_allowlist_unparseable_ip_returns_none():
    assert bc._ip_in_allowlist("not-an-ip", ("192.0.2.0/24",)) is None


def test_ip_allowlist_v4_v6_mismatch_no_hit():
    assert bc._ip_in_allowlist("192.0.2.1", ("2001:db8::/48",)) is None
    assert bc._ip_in_allowlist("2001:db8::1", ("192.0.2.0/24",)) is None


def test_ip_allowlist_empty_inputs():
    assert bc._ip_in_allowlist(None, ()) is None
    assert bc._ip_in_allowlist("192.0.2.1", ()) is None
    assert bc._ip_in_allowlist("", ("192.0.2.0/24",)) is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 6 — Test-token constant-time compare
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_test_token_matches_happy_path():
    expected = "a" * 32
    assert bc._test_token_matches(expected, expected) is True


def test_test_token_matches_none_inputs_return_false():
    assert bc._test_token_matches(None, "a" * 32) is False
    assert bc._test_token_matches("a" * 32, None) is False
    assert bc._test_token_matches("", "a" * 32) is False
    assert bc._test_token_matches("a" * 32, "") is False


def test_test_token_matches_short_envvar_returns_false():
    """``< 32 chars`` is treated as unset on the env side."""
    assert bc._test_token_matches("a" * 32, "a" * 31) is False


def test_test_token_matches_constant_time():
    """secrets.compare_digest is what we use."""
    src = pathlib.Path(bc.__file__).read_text()
    assert "secrets.compare_digest" in src
    # Forbid plain == comparison anywhere near the test_token branch.
    # (If this fails, refactor must keep the constant-time invariant.)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 7 — Provider response parsing (score normalisation)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_parse_response_turnstile_uses_score_field():
    payload = {"success": True, "score": 0.87, "action": "login", "hostname": "x"}
    parsed = bc._parse_response(bc.Provider.TURNSTILE, payload)
    assert parsed.success is True
    assert parsed.score == 0.87
    assert parsed.action == "login"


def test_parse_response_recaptcha_v3_uses_score_field():
    payload = {"success": True, "score": 0.42, "action": "signup", "hostname": "x"}
    parsed = bc._parse_response(bc.Provider.RECAPTCHA_V3, payload)
    assert parsed.score == 0.42


def test_parse_response_recaptcha_v2_binary_score():
    """v2 has no score in the response — success=True ⇒ 1.0."""
    payload = {"success": True, "hostname": "x"}
    parsed = bc._parse_response(bc.Provider.RECAPTCHA_V2, payload)
    assert parsed.score == 1.0


def test_parse_response_recaptcha_v2_failure_binary_zero():
    payload = {"success": False, "error-codes": ["invalid-input-response"]}
    parsed = bc._parse_response(bc.Provider.RECAPTCHA_V2, payload)
    assert parsed.score == 0.0
    assert parsed.error_codes == ("invalid-input-response",)


def test_parse_response_hcaptcha_binary_score():
    payload = {"success": True, "hostname": "x", "credit": True}
    parsed = bc._parse_response(bc.Provider.HCAPTCHA, payload)
    assert parsed.score == 1.0


def test_parse_response_score_clamped_to_unit_interval():
    """Defensive: vendor sends score=1.5 or score=-0.1 → clamp to [0,1]."""
    payload = {"success": True, "score": 1.5}
    parsed = bc._parse_response(bc.Provider.RECAPTCHA_V3, payload)
    assert parsed.score == 1.0
    payload = {"success": True, "score": -0.1}
    parsed = bc._parse_response(bc.Provider.RECAPTCHA_V3, payload)
    assert parsed.score == 0.0


def test_parse_response_error_codes_list_normalised():
    payload = {"success": False, "error-codes": ["invalid-input-secret", "timeout-or-duplicate"]}
    parsed = bc._parse_response(bc.Provider.TURNSTILE, payload)
    assert parsed.error_codes == ("invalid-input-secret", "timeout-or-duplicate")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 8 — Phase-aware classify_outcome
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _resp(success: bool, score: float, *, error_codes: tuple[str, ...] = ()):
    return bc.ProviderResponse(
        success=success,
        score=score,
        action=None,
        hostname=None,
        raw={},
        error_codes=error_codes,
    )


@pytest.mark.parametrize("phase", [1, 2, 3])
def test_classify_pass_high_score_all_phases(phase):
    result = bc.classify_outcome(
        _resp(True, 0.9), provider=bc.Provider.RECAPTCHA_V3, phase=phase,
    )
    assert result.outcome == bc.OUTCOME_PASS
    assert result.allow is True
    assert result.score == 0.9
    assert result.audit_event == bc.EVENT_BOT_CHALLENGE_PASS


@pytest.mark.parametrize("phase", [1, 2])
def test_classify_phase1_2_lowscore_fail_open(phase):
    """AS.0.5 §2.2 / §2.3 — Phase 1/2 lowscore is unverified, allow=True."""
    result = bc.classify_outcome(
        _resp(True, 0.1), provider=bc.Provider.RECAPTCHA_V3, phase=phase,
    )
    assert result.outcome == bc.OUTCOME_UNVERIFIED_LOWSCORE
    assert result.allow is True
    assert result.audit_event == bc.EVENT_BOT_CHALLENGE_UNVERIFIED_LOWSCORE


def test_classify_phase3_lowscore_fail_closed():
    """AS.0.5 §2.4 — Phase 3 confirmed lowscore is blocked, allow=False."""
    result = bc.classify_outcome(
        _resp(True, 0.1), provider=bc.Provider.RECAPTCHA_V3, phase=3,
    )
    assert result.outcome == bc.OUTCOME_BLOCKED_LOWSCORE
    assert result.allow is False
    assert result.audit_event == bc.EVENT_BOT_CHALLENGE_BLOCKED_LOWSCORE


@pytest.mark.parametrize("phase", [1, 2, 3])
def test_classify_server_error_fail_open_all_phases(phase):
    """AS.0.5 §2.4 row 3 — server-side verify error is fail-open even
    in Phase 3 (our-side fault, not user fault)."""
    result = bc.classify_outcome(
        _resp(False, 0.0, error_codes=("invalid-input-secret",)),
        provider=bc.Provider.TURNSTILE, phase=phase,
    )
    assert result.outcome == bc.OUTCOME_UNVERIFIED_SERVERERR
    assert result.allow is True
    assert result.audit_event == bc.EVENT_BOT_CHALLENGE_UNVERIFIED_SERVERERR
    assert result.audit_metadata["error_kind"] == "4xx_invalid_token"


def test_classify_threshold_boundary():
    """Score == threshold (0.5) → pass.  Score == threshold-epsilon → low."""
    result_at = bc.classify_outcome(
        _resp(True, 0.5), provider=bc.Provider.RECAPTCHA_V3, phase=1,
    )
    assert result_at.outcome == bc.OUTCOME_PASS
    result_below = bc.classify_outcome(
        _resp(True, 0.499), provider=bc.Provider.RECAPTCHA_V3, phase=1,
    )
    assert result_below.outcome == bc.OUTCOME_UNVERIFIED_LOWSCORE


def test_classify_widget_action_propagated_to_metadata():
    result = bc.classify_outcome(
        _resp(True, 0.9), provider=bc.Provider.TURNSTILE, phase=1,
        widget_action="signup",
    )
    assert result.audit_metadata["widget_action"] == "signup"


def test_classify_phase_must_be_1_2_3():
    with pytest.raises(ValueError):
        bc.classify_outcome(_resp(True, 0.9), provider=bc.Provider.TURNSTILE, phase=0)
    with pytest.raises(ValueError):
        bc.classify_outcome(_resp(True, 0.9), provider=bc.Provider.TURNSTILE, phase=4)


def test_classify_error_kind_dispatch():
    """error_kind label dispatches by AS.0.5 §3 metadata schema."""
    # timeout-or-duplicate → "timeout"
    r = bc.classify_outcome(
        _resp(False, 0.0, error_codes=("timeout-or-duplicate",)),
        provider=bc.Provider.HCAPTCHA, phase=1,
    )
    assert r.audit_metadata["error_kind"] == "timeout"
    # http-503 → "5xx"
    r = bc.classify_outcome(
        _resp(False, 0.0, error_codes=("http-503",)),
        provider=bc.Provider.TURNSTILE, phase=1,
    )
    assert r.audit_metadata["error_kind"] == "5xx"
    # missing-input-response → "4xx_invalid_token"
    r = bc.classify_outcome(
        _resp(False, 0.0, error_codes=("missing-input-response",)),
        provider=bc.Provider.TURNSTILE, phase=1,
    )
    assert r.audit_metadata["error_kind"] == "4xx_invalid_token"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 9 + 10 — Top-level verify() + provider HTTP integration
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class _FakePoolResponse:
    """Stand-in for httpx.Response — supports ``.status_code`` + ``.json()``."""

    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class _FakeAsyncClient:
    """Minimal stand-in for ``httpx.AsyncClient`` — captures the
    ``.post`` call args and returns a programmable response."""

    def __init__(self, response: _FakePoolResponse):
        self._response = response
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, *, data=None, timeout=None):  # noqa: D401
        self.calls.append((url, dict(data or {})))
        return self._response


@pytest.mark.asyncio
async def test_verify_provider_turnstile_happy_path():
    fake = _FakeAsyncClient(_FakePoolResponse(200, {
        "success": True, "score": 0.85, "action": "login", "hostname": "x",
    }))
    parsed = await bc.verify_provider(
        provider=bc.Provider.TURNSTILE,
        token="tok",
        secret="s" * 40,
        http_client=fake,
    )
    assert parsed.success is True
    assert parsed.score == 0.85
    assert parsed.action == "login"
    assert fake.calls == [(
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        {"secret": "s" * 40, "response": "tok"},
    )]


@pytest.mark.asyncio
async def test_verify_provider_recaptcha_v2_binary_success():
    fake = _FakeAsyncClient(_FakePoolResponse(200, {
        "success": True, "hostname": "x",
    }))
    parsed = await bc.verify_provider(
        provider=bc.Provider.RECAPTCHA_V2,
        token="tok", secret="s" * 40, http_client=fake,
    )
    assert parsed.success is True
    assert parsed.score == 1.0


@pytest.mark.asyncio
async def test_verify_provider_recaptcha_v3_continuous_score():
    fake = _FakeAsyncClient(_FakePoolResponse(200, {
        "success": True, "score": 0.3, "action": "signup", "hostname": "x",
    }))
    parsed = await bc.verify_provider(
        provider=bc.Provider.RECAPTCHA_V3,
        token="tok", secret="s" * 40, http_client=fake,
    )
    assert parsed.success is True
    assert parsed.score == 0.3
    assert parsed.action == "signup"


@pytest.mark.asyncio
async def test_verify_provider_hcaptcha_binary_success():
    fake = _FakeAsyncClient(_FakePoolResponse(200, {
        "success": True, "hostname": "x", "credit": True,
    }))
    parsed = await bc.verify_provider(
        provider=bc.Provider.HCAPTCHA,
        token="tok", secret="s" * 40, http_client=fake,
    )
    assert parsed.success is True
    assert parsed.score == 1.0


@pytest.mark.asyncio
async def test_verify_provider_remote_ip_forwarded():
    fake = _FakeAsyncClient(_FakePoolResponse(200, {"success": True}))
    await bc.verify_provider(
        provider=bc.Provider.TURNSTILE,
        token="tok", secret="s" * 40,
        remote_ip="203.0.113.1",
        http_client=fake,
    )
    assert fake.calls[0][1].get("remoteip") == "203.0.113.1"


@pytest.mark.asyncio
async def test_verify_provider_action_mismatch_demoted():
    """If expected_action != response.action, demote to failure with
    `action-mismatch` error code (anti-replay across forms)."""
    fake = _FakeAsyncClient(_FakePoolResponse(200, {
        "success": True, "score": 0.9, "action": "signup", "hostname": "x",
    }))
    parsed = await bc.verify_provider(
        provider=bc.Provider.RECAPTCHA_V3,
        token="tok", secret="s" * 40,
        expected_action="login",
        http_client=fake,
    )
    assert parsed.success is False
    assert "action-mismatch" in parsed.error_codes


@pytest.mark.asyncio
async def test_verify_provider_action_mismatch_skipped_for_v2_no_action():
    """v2 doesn't ship action; expected_action is informational only."""
    fake = _FakeAsyncClient(_FakePoolResponse(200, {
        "success": True, "hostname": "x",
    }))
    parsed = await bc.verify_provider(
        provider=bc.Provider.RECAPTCHA_V2,
        token="tok", secret="s" * 40,
        expected_action="login",
        http_client=fake,
    )
    assert parsed.success is True


@pytest.mark.asyncio
async def test_verify_provider_4xx_returns_synthetic_failure():
    fake = _FakeAsyncClient(_FakePoolResponse(400, {"error": "bad request"}))
    parsed = await bc.verify_provider(
        provider=bc.Provider.TURNSTILE,
        token="tok", secret="s" * 40, http_client=fake,
    )
    assert parsed.success is False
    assert "http-400" in parsed.error_codes


@pytest.mark.asyncio
async def test_verify_provider_5xx_raises():
    fake = _FakeAsyncClient(_FakePoolResponse(503, {}))
    with pytest.raises(bc.BotChallengeError):
        await bc.verify_provider(
            provider=bc.Provider.TURNSTILE,
            token="tok", secret="s" * 40, http_client=fake,
        )


@pytest.mark.asyncio
async def test_verify_provider_empty_secret_raises():
    with pytest.raises(bc.ProviderConfigError):
        await bc.verify_provider(
            provider=bc.Provider.TURNSTILE,
            token="tok", secret="",
        )


@pytest.mark.asyncio
async def test_verify_provider_empty_token_short_circuits_failure():
    """Empty token doesn't go to the wire — synthesised
    missing-input-response failure."""
    fake = _FakeAsyncClient(_FakePoolResponse(200, {"success": True}))
    parsed = await bc.verify_provider(
        provider=bc.Provider.TURNSTILE,
        token="",
        secret="s" * 40,
        http_client=fake,
    )
    assert parsed.success is False
    assert "missing-input-response" in parsed.error_codes
    assert fake.calls == []  # never hit the wire


@pytest.mark.asyncio
async def test_verify_orchestrator_knob_off_passthrough():
    """AS.0.8: knob-off ⇒ passthrough, no audit, no provider call."""
    fake = _FakeAsyncClient(_FakePoolResponse(200, {"success": True}))
    ctx = bc.VerifyContext(
        provider=bc.Provider.TURNSTILE, token="tok", secret="s" * 40,
    )
    with patch.object(bc, "is_enabled", return_value=False):
        result = await bc.verify(ctx, http_client=fake)
    assert result.outcome == bc.OUTCOME_PASS
    assert result.allow is True
    assert result.audit_metadata.get("passthrough_reason") == "knob_off"
    assert fake.calls == []  # no provider call


@pytest.mark.asyncio
async def test_verify_orchestrator_bypass_short_circuit():
    """If a bypass axis matches, provider verify must NOT be called."""
    fake = _FakeAsyncClient(_FakePoolResponse(200, {"success": True}))
    ctx = bc.VerifyContext(
        provider=bc.Provider.TURNSTILE,
        token="tok", secret="s" * 40,
        bypass=bc.BypassContext(caller_kind="apikey_omni", api_key_id="ak-1"),
    )
    result = await bc.verify(ctx, http_client=fake)
    assert result.outcome == bc.OUTCOME_BYPASS_APIKEY
    assert result.allow is True
    assert fake.calls == []


@pytest.mark.asyncio
async def test_verify_orchestrator_missing_secret_fail_open():
    """Operator forgot to set OMNISIGHT_*_SECRET: fail-open, not lock-out."""
    ctx = bc.VerifyContext(provider=bc.Provider.TURNSTILE, token="tok", secret=None)
    result = await bc.verify(ctx)
    assert result.outcome == bc.OUTCOME_UNVERIFIED_SERVERERR
    assert result.allow is True
    assert result.audit_metadata["error_kind"] == "config_missing_secret"


@pytest.mark.asyncio
async def test_verify_orchestrator_full_chain_pass():
    fake = _FakeAsyncClient(_FakePoolResponse(200, {
        "success": True, "score": 0.9, "action": "login", "hostname": "x",
    }))
    ctx = bc.VerifyContext(
        provider=bc.Provider.RECAPTCHA_V3,
        token="tok", secret="s" * 40,
        phase=1, widget_action="login",
    )
    result = await bc.verify(ctx, http_client=fake)
    assert result.outcome == bc.OUTCOME_PASS
    assert result.allow is True
    assert result.audit_metadata["widget_action"] == "login"


@pytest.mark.asyncio
async def test_verify_orchestrator_phase3_blocks_lowscore():
    fake = _FakeAsyncClient(_FakePoolResponse(200, {
        "success": True, "score": 0.1, "action": "login", "hostname": "x",
    }))
    ctx = bc.VerifyContext(
        provider=bc.Provider.RECAPTCHA_V3,
        token="tok", secret="s" * 40,
        phase=3, widget_action="login",
    )
    result = await bc.verify(ctx, http_client=fake)
    assert result.outcome == bc.OUTCOME_BLOCKED_LOWSCORE
    assert result.allow is False


@pytest.mark.asyncio
async def test_verify_orchestrator_transport_error_fail_open():
    """Network drops: fail-open per AS.0.5 §2.4 row 3."""
    class _BoomClient:
        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("network down")
    ctx = bc.VerifyContext(
        provider=bc.Provider.TURNSTILE, token="tok", secret="s" * 40,
    )
    result = await bc.verify(ctx, http_client=_BoomClient())
    assert result.outcome == bc.OUTCOME_UNVERIFIED_SERVERERR
    assert result.allow is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 11 — Knob-off short-circuit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_passthrough_shape():
    result = bc.passthrough(reason="dev_mode")
    assert result.outcome == bc.OUTCOME_PASS
    assert result.allow is True
    assert result.score == 1.0
    assert result.provider is None
    assert result.audit_metadata["passthrough_reason"] == "dev_mode"


def test_is_enabled_default_true_when_settings_field_absent():
    """Forward-promotion guard — works before AS.3.x lands the field."""
    # Make settings.as_enabled invisible (simulate forward-promotion)
    # by patching the settings object's getattr behaviour.
    class _MockSettings:
        pass
    with patch("backend.config.settings", _MockSettings()):
        assert bc.is_enabled() is True


def test_is_enabled_respects_explicit_false():
    class _MockSettings:
        as_enabled = False
    with patch("backend.config.settings", _MockSettings()):
        assert bc.is_enabled() is False


def test_is_enabled_respects_explicit_true():
    class _MockSettings:
        as_enabled = True
    with patch("backend.config.settings", _MockSettings()):
        assert bc.is_enabled() is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 12 — Module-global state audit (per SOP §1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_no_module_level_mutable_containers():
    """SOP §1: only frozen dataclasses, tuples, frozensets, str at
    module level.  No bare ``list`` / ``dict`` / ``set`` (other than
    constants whose names start with ``_PROVIDER_`` mapping)."""
    for name, value in vars(bc).items():
        if name.startswith("_") or name in {"logger"}:
            continue
        if isinstance(value, (list, dict, set)):
            raise AssertionError(
                f"module-level mutable {type(value).__name__} {name!r} "
                "violates SOP §1 — convert to tuple/frozenset"
            )


def test_constants_stable_across_reload():
    """Reloading the module yields byte-equal event constants."""
    pre = (
        bc.EVENT_BOT_CHALLENGE_PASS,
        bc.EVENT_BOT_CHALLENGE_BYPASS_APIKEY,
        bc.EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P1_TO_P2,
    )
    importlib.reload(bc)
    post = (
        bc.EVENT_BOT_CHALLENGE_PASS,
        bc.EVENT_BOT_CHALLENGE_BYPASS_APIKEY,
        bc.EVENT_BOT_CHALLENGE_PHASE_ADVANCE_P1_TO_P2,
    )
    assert pre == post


def test_module_import_no_io_at_top_level():
    """Smoke: importing the module mustn't read env / open files /
    spawn subprocesses / open DB connections."""
    src = pathlib.Path(bc.__file__).read_text()
    forbidden_at_top = [
        "open(",  # file IO
        "subprocess",
        "os.environ",  # env reads belong in is_enabled() lazy
        "asyncpg.connect",
        "sqlite3.connect",
    ]
    for marker in forbidden_at_top:
        # Allow inside function bodies (indented).  Forbid at column 0.
        for line in src.splitlines():
            if line.startswith(marker):
                raise AssertionError(
                    f"top-level {marker!r} in bot_challenge.py — "
                    "violates SOP §1 (module import is not free of IO)"
                )


def test_pre_commit_compat_fingerprint_grep():
    """SOP Step 3 line 47: fingerprint grep for compat-shim leftovers."""
    import re
    fingerprints = [
        r"_conn\(\)",
        r"await conn\.commit\(\)",
        r"datetime\('now'\)",
        r"VALUES.*\?[,)]",
    ]
    src = pathlib.Path(bc.__file__).read_text()
    for fp in fingerprints:
        for line in src.splitlines():
            if re.search(fp, line):
                raise AssertionError(
                    f"fingerprint hit in bot_challenge.py: {line!r}"
                )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 13 — Drift guards across module / plan / outcome map
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_outcome_to_event_lookup_is_total_and_injective():
    """Every outcome maps to a unique event; no event is mapped twice."""
    seen_events = set()
    for outcome in bc.ALL_OUTCOMES:
        event = bc.event_for_outcome(outcome)
        assert event in bc.ALL_BOT_CHALLENGE_EVENTS
        assert event not in seen_events, (
            f"event {event!r} mapped twice"
        )
        seen_events.add(event)


def test_provider_enum_values_are_lowercase_no_dashes():
    """TS twin compatibility: enum string values are
    snake/lowercase, no spaces / dashes."""
    for p in bc.Provider:
        assert p.value == p.value.lower()
        assert " " not in p.value
        assert "-" not in p.value


def test_all_module_exports_present_in_dunder_all():
    """``__all__`` covers every public symbol the docstring promises."""
    promised = {
        "Provider", "verify", "verify_provider", "classify_outcome",
        "evaluate_bypass", "passthrough", "is_enabled", "pick_provider",
        "BotChallengeResult", "BypassReason", "BypassContext",
        "ProviderResponse", "VerifyContext",
        "BotChallengeError", "ProviderConfigError", "InvalidProviderError",
        "ALL_BOT_CHALLENGE_EVENTS", "ALL_OUTCOMES", "event_for_outcome",
        "TEST_TOKEN_HEADER", "DEFAULT_SCORE_THRESHOLD",
    }
    assert promised.issubset(set(bc.__all__))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 14 — Action-mismatch demotion (anti-replay)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_action_mismatch_demotes_to_failure_not_pass():
    """A signup-form replayed token shipped to a login form must
    not score-pass."""
    fake = _FakeAsyncClient(_FakePoolResponse(200, {
        "success": True, "score": 0.9, "action": "signup", "hostname": "x",
    }))
    parsed = await bc.verify_provider(
        provider=bc.Provider.RECAPTCHA_V3,
        token="t", secret="s" * 40,
        expected_action="login",
        http_client=fake,
    )
    assert parsed.success is False
    assert parsed.score == 0.0
    assert "action-mismatch" in parsed.error_codes


@pytest.mark.asyncio
async def test_action_match_passes():
    fake = _FakeAsyncClient(_FakePoolResponse(200, {
        "success": True, "score": 0.9, "action": "login", "hostname": "x",
    }))
    parsed = await bc.verify_provider(
        provider=bc.Provider.RECAPTCHA_V3,
        token="t", secret="s" * 40,
        expected_action="login",
        http_client=fake,
    )
    assert parsed.success is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 15 — Server-error fail-open invariant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize("phase", [1, 2, 3])
def test_server_error_always_fail_open(phase):
    """AS.0.5 §2.4 row 3 — server-side verify error is fail-open in
    EVERY phase including Phase 3."""
    result = bc.classify_outcome(
        _resp(False, 0.0, error_codes=("invalid-input-secret",)),
        provider=bc.Provider.TURNSTILE, phase=phase,
    )
    assert result.allow is True


def test_pick_provider_default_turnstile():
    """AS.3.1 placeholder — default Turnstile.  AS.3.3 will replace."""
    assert bc.pick_provider() == bc.Provider.TURNSTILE


def test_pick_provider_caller_override():
    assert bc.pick_provider(default=bc.Provider.HCAPTCHA) == bc.Provider.HCAPTCHA

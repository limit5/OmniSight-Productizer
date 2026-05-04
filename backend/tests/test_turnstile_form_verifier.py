"""AS.6.3 — backend.security.turnstile_form_verifier contract tests.

Validates the universal Turnstile backend-verify wiring helper that
the four OmniSight self-forms (login / signup / password-reset /
contact) share per AS.0.5 §6.1 acceptance criteria.

Test families
─────────────
1. Form action / form path constants — 4 actions + 4 paths frozen,
   ``SUPPORTED_*`` frozenset shape, ``form_path_for_action``
   dispatch + unknown action raises.
2. Cross-module drift — :data:`SUPPORTED_FORM_PATHS` byte-equal
   :data:`backend.security.honeypot._FORM_PREFIXES` keys (AS.5.2
   dashboard natural-join invariant per AS.4.1 §4.1).
3. Token surface constants — body field name + header name pinned.
4. Phase knob — ``current_phase`` reads
   ``OMNISIGHT_BOT_CHALLENGE_PHASE`` env, default 1, fallback on
   invalid / out-of-range values.
5. AS.0.8 single-knob — ``is_enabled`` mirrors
   :func:`bot_challenge.is_enabled`; knob-off → ``verify_form_token``
   returns AS.3.1 passthrough result with no audit emit / no env
   reads / no HTTP.
6. Token extraction — body payload wins over header, missing both
   returns ``None``, whitespace stripped.
7. Bypass context build — client_ip from CF header / request.client,
   caller_kind from request.state, test-token from header, tenant
   IP allowlist normalised to immutable tuple.
8. Provider selection / secret resolution — pick_form_provider
   honours CF-IPCountry, ``resolve_provider_secret`` reads the right
   env via :func:`bot_challenge.secret_env_for`.
9. End-to-end ``verify_form_token`` — fail-open Phase 1
   (unverified_servererr allow=True), Phase 3 confirmed low-score
   (blocked_lowscore allow=False), bypass axis fires before verify,
   AS.5.1 rollup row fired with correct kind/reason.
10. ``verify_form_token_or_reject`` — Phase 3 blocked → raises
    BotChallengeRejected with canonical 429 + bot_challenge_failed
    code; allow paths return result.
11. Module-global state audit (SOP §1) — no top-level mutable
    container; importing is side-effect free.
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from backend.security import auth_event, bot_challenge, honeypot
from backend.security import turnstile_form_verifier as tv


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _fake_request(
    *,
    headers: dict[str, str] | None = None,
    client_host: str | None = "203.0.113.5",
    state: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """Minimal stand-in for :class:`fastapi.Request` that the helper
    consumes — only the headers + client + state surfaces are read."""
    hdrs = {k.lower(): v for k, v in (headers or {}).items()}

    class _Headers:
        def __init__(self, items: dict[str, str]) -> None:
            self._items = items

        def get(self, key: str, default: str | None = None) -> str | None:
            return self._items.get(key.lower(), default)

    client = SimpleNamespace(host=client_host) if client_host else None
    state_obj = SimpleNamespace(**(state or {}))
    return SimpleNamespace(
        headers=_Headers(hdrs),
        client=client,
        state=state_obj,
    )


class _FakeHttpClient:
    """Minimal httpx.AsyncClient stand-in that returns a pre-built
    response for the next ``post`` call. Preserves the call args
    so tests can assert on URL + form payload."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, *, data: dict[str, Any], timeout: float) -> httpx.Response:  # noqa: ARG002
        self.calls.append((url, dict(data)))
        return self.response


def _siteverify_response(payload: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=__import__("json").dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — Form action / form path constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_form_action_constants_pinned():
    assert tv.FORM_ACTION_LOGIN == "login"
    assert tv.FORM_ACTION_SIGNUP == "signup"
    assert tv.FORM_ACTION_PASSWORD_RESET == "pwreset"
    assert tv.FORM_ACTION_CONTACT == "contact"


def test_supported_form_actions_is_frozen_4_set():
    assert isinstance(tv.SUPPORTED_FORM_ACTIONS, frozenset)
    assert tv.SUPPORTED_FORM_ACTIONS == {
        "login", "signup", "pwreset", "contact",
    }


def test_form_path_constants_pinned():
    assert tv.FORM_PATH_LOGIN == "/api/v1/auth/login"
    assert tv.FORM_PATH_SIGNUP == "/api/v1/auth/signup"
    assert tv.FORM_PATH_PASSWORD_RESET == "/api/v1/auth/password-reset"
    assert tv.FORM_PATH_CONTACT == "/api/v1/auth/contact"


def test_form_path_for_action_dispatch():
    assert tv.form_path_for_action(tv.FORM_ACTION_LOGIN) == tv.FORM_PATH_LOGIN
    assert tv.form_path_for_action(tv.FORM_ACTION_SIGNUP) == tv.FORM_PATH_SIGNUP
    assert tv.form_path_for_action(tv.FORM_ACTION_PASSWORD_RESET) == tv.FORM_PATH_PASSWORD_RESET
    assert tv.form_path_for_action(tv.FORM_ACTION_CONTACT) == tv.FORM_PATH_CONTACT


def test_form_path_for_action_unknown_raises():
    with pytest.raises(ValueError, match="unknown form_action"):
        tv.form_path_for_action("not-a-real-action")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — Cross-module drift guard with honeypot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_supported_form_paths_aligned_with_honeypot():
    """AS.4.1 §4.1 invariant — captcha + honeypot form_path SoT must
    match byte-for-byte so the AS.5.2 dashboard can natural-join the
    rollup rows."""
    assert tv.SUPPORTED_FORM_PATHS == frozenset(honeypot._FORM_PREFIXES.keys())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — Token surface constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_token_body_field_pinned():
    assert tv.TURNSTILE_TOKEN_BODY_FIELD == "turnstile_token"


def test_token_header_pinned():
    assert tv.TURNSTILE_TOKEN_HEADER == "X-Turnstile-Token"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 4 — Phase knob
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_default_phase_is_one(monkeypatch):
    monkeypatch.delenv(tv.PHASE_ENV_VAR, raising=False)
    assert tv.current_phase() == 1


@pytest.mark.parametrize("env_value, expected", [
    ("1", 1),
    ("2", 2),
    ("3", 3),
    (" 2 ", 2),
])
def test_phase_env_parses_valid(monkeypatch, env_value, expected):
    monkeypatch.setenv(tv.PHASE_ENV_VAR, env_value)
    assert tv.current_phase() == expected


@pytest.mark.parametrize("env_value", ["0", "4", "-1", "not-a-number", ""])
def test_phase_env_invalid_falls_back_to_default(monkeypatch, env_value):
    monkeypatch.setenv(tv.PHASE_ENV_VAR, env_value)
    assert tv.current_phase() == tv.DEFAULT_BOT_CHALLENGE_PHASE


def test_phase_env_var_name_pinned():
    assert tv.PHASE_ENV_VAR == "OMNISIGHT_BOT_CHALLENGE_PHASE"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 5 — AS.0.8 single-knob
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_is_enabled_default_true():
    """AS.0.8 §3.1 forward-promotion guard: when settings.as_enabled
    field hasn't landed yet, the helper still defaults to True (AS
    active) so existing deployments don't accidentally disable AS."""
    assert tv.is_enabled() is True


def test_knob_off_returns_passthrough_no_audit():
    """AS.0.8 §3.1 noop matrix — knob-false ⇒ helper returns AS.3.1
    passthrough, no AS.5.1 rollup row, no provider secret env read,
    no HTTP."""
    request = _fake_request()
    pass_emit = AsyncMock()
    fail_emit = AsyncMock()
    with patch.object(tv, "is_enabled", return_value=False), \
         patch.object(auth_event, "emit_bot_challenge_pass", pass_emit), \
         patch.object(auth_event, "emit_bot_challenge_fail", fail_emit):
        result = _run(tv.verify_form_token(
            tv.FORM_ACTION_LOGIN,
            "any-token",
            request=request,
        ))
    assert result.outcome == bot_challenge.OUTCOME_PASS
    assert result.allow is True
    assert result.audit_metadata.get("passthrough_reason") == "knob_off"
    pass_emit.assert_not_awaited()
    fail_emit.assert_not_awaited()


def test_knob_off_or_reject_returns_passthrough():
    """verify_form_token_or_reject is a thin wrapper — knob-off path
    must NOT raise BotChallengeRejected even though the wrapper's
    job is to raise on reject. passthrough has allow=True ⇒ no reject."""
    request = _fake_request()
    with patch.object(tv, "is_enabled", return_value=False):
        result = _run(tv.verify_form_token_or_reject(
            tv.FORM_ACTION_LOGIN,
            None,
            request=request,
        ))
    assert result.allow is True
    assert result.outcome == bot_challenge.OUTCOME_PASS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 6 — Token extraction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_extract_token_body_wins_over_header():
    request = _fake_request(headers={tv.TURNSTILE_TOKEN_HEADER: "header-tok"})
    body = {tv.TURNSTILE_TOKEN_BODY_FIELD: "body-tok"}
    assert tv.extract_token_from_request(request, body_payload=body) == "body-tok"


def test_extract_token_header_fallback():
    request = _fake_request(headers={tv.TURNSTILE_TOKEN_HEADER: "header-tok"})
    assert tv.extract_token_from_request(request, body_payload={}) == "header-tok"


def test_extract_token_missing_returns_none():
    request = _fake_request()
    assert tv.extract_token_from_request(request, body_payload={}) is None


def test_extract_token_strips_whitespace():
    request = _fake_request(headers={tv.TURNSTILE_TOKEN_HEADER: "  spaced  "})
    assert tv.extract_token_from_request(request, body_payload={}) == "spaced"


def test_extract_token_empty_body_field_falls_through():
    request = _fake_request(headers={tv.TURNSTILE_TOKEN_HEADER: "header-tok"})
    body = {tv.TURNSTILE_TOKEN_BODY_FIELD: "  "}  # whitespace-only ignored
    assert tv.extract_token_from_request(request, body_payload=body) == "header-tok"


def test_extract_token_non_string_body_field_falls_through():
    request = _fake_request(headers={tv.TURNSTILE_TOKEN_HEADER: "header-tok"})
    body = {tv.TURNSTILE_TOKEN_BODY_FIELD: 12345}  # type: ignore[dict-item]
    assert tv.extract_token_from_request(request, body_payload=body) == "header-tok"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 7 — Bypass context build
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_bypass_context_cf_ip_wins_over_client_host():
    request = _fake_request(
        headers={"cf-connecting-ip": "198.51.100.7"},
        client_host="10.0.0.1",
    )
    ctx = tv.build_bypass_context(request, form_path=tv.FORM_PATH_LOGIN)
    assert ctx.client_ip == "198.51.100.7"


def test_bypass_context_falls_back_to_client_host():
    request = _fake_request(client_host="10.0.0.1")
    ctx = tv.build_bypass_context(request, form_path=tv.FORM_PATH_LOGIN)
    assert ctx.client_ip == "10.0.0.1"


def test_bypass_context_caller_kind_from_state():
    request = _fake_request(state={"caller_kind": "apikey_omni"})
    ctx = tv.build_bypass_context(request, form_path=tv.FORM_PATH_LOGIN)
    assert ctx.caller_kind == "apikey_omni"


def test_bypass_context_no_caller_kind_when_state_missing():
    request = _fake_request()
    ctx = tv.build_bypass_context(request, form_path=tv.FORM_PATH_LOGIN)
    assert ctx.caller_kind is None


def test_bypass_context_test_token_header_picked_up(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_BOT_CHALLENGE_TEST_TOKEN", "x" * 32)
    request = _fake_request(headers={bot_challenge.TEST_TOKEN_HEADER: "x" * 32})
    ctx = tv.build_bypass_context(request, form_path=tv.FORM_PATH_LOGIN)
    assert ctx.test_token_header_value == "x" * 32
    assert ctx.test_token_expected == "x" * 32


def test_bypass_context_tenant_ip_allowlist_normalised():
    request = _fake_request()
    ctx = tv.build_bypass_context(
        request,
        form_path=tv.FORM_PATH_LOGIN,
        tenant_ip_allowlist=["10.0.0.0/8", "  ", "", "192.168.0.0/16"],
    )
    assert ctx.tenant_ip_allowlist == ("10.0.0.0/8", "192.168.0.0/16")


def test_bypass_context_widget_action_propagates():
    request = _fake_request()
    ctx = tv.build_bypass_context(
        request,
        form_path=tv.FORM_PATH_LOGIN,
        widget_action=tv.FORM_ACTION_LOGIN,
    )
    assert ctx.widget_action == tv.FORM_ACTION_LOGIN


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 8 — Provider selection / secret resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_pick_form_provider_default_is_turnstile():
    request = _fake_request()
    assert tv.pick_form_provider(request) is bot_challenge.Provider.TURNSTILE


def test_pick_form_provider_gdpr_region_picks_hcaptcha():
    request = _fake_request(headers={"cf-ipcountry": "DE"})
    assert tv.pick_form_provider(request) is bot_challenge.Provider.HCAPTCHA


def test_pick_form_provider_override_wins():
    request = _fake_request(headers={"cf-ipcountry": "DE"})
    assert tv.pick_form_provider(
        request,
        override=bot_challenge.Provider.TURNSTILE,
    ) is bot_challenge.Provider.TURNSTILE


def test_pick_form_provider_google_ecosystem_hint():
    request = _fake_request()
    assert tv.pick_form_provider(
        request,
        ecosystem_hints=("google",),
    ) is bot_challenge.Provider.RECAPTCHA_V3


def test_resolve_provider_secret_reads_env(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_TURNSTILE_SECRET", "ts-secret-x")
    monkeypatch.setenv("OMNISIGHT_RECAPTCHA_SECRET", "rc-secret-y")
    monkeypatch.setenv("OMNISIGHT_HCAPTCHA_SECRET", "hc-secret-z")
    assert tv.resolve_provider_secret(bot_challenge.Provider.TURNSTILE) == "ts-secret-x"
    assert tv.resolve_provider_secret(bot_challenge.Provider.RECAPTCHA_V3) == "rc-secret-y"
    assert tv.resolve_provider_secret(bot_challenge.Provider.RECAPTCHA_V2) == "rc-secret-y"
    assert tv.resolve_provider_secret(bot_challenge.Provider.HCAPTCHA) == "hc-secret-z"


def test_resolve_provider_secret_missing_returns_empty(monkeypatch):
    monkeypatch.delenv("OMNISIGHT_TURNSTILE_SECRET", raising=False)
    assert tv.resolve_provider_secret(bot_challenge.Provider.TURNSTILE) == ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 9 — End-to-end verify_form_token
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_verify_form_token_happy_path_phase_1(monkeypatch):
    """Vendor returns success+score=0.9 → OUTCOME_PASS, AS.5.1 rollup
    fires `auth.bot_challenge_pass` with kind='verified' + score."""
    monkeypatch.setenv("OMNISIGHT_TURNSTILE_SECRET", "secret-x")
    monkeypatch.delenv(tv.PHASE_ENV_VAR, raising=False)
    request = _fake_request()
    fake_client = _FakeHttpClient(_siteverify_response({
        "success": True, "score": 0.9, "action": "login",
    }))

    pass_emit = AsyncMock()
    fail_emit = AsyncMock()
    with patch.object(auth_event, "emit_bot_challenge_pass", pass_emit), \
         patch.object(auth_event, "emit_bot_challenge_fail", fail_emit):
        result = _run(tv.verify_form_token(
            tv.FORM_ACTION_LOGIN,
            "valid-token",
            request=request,
            http_client=fake_client,
        ))

    assert result.outcome == bot_challenge.OUTCOME_PASS
    assert result.allow is True
    assert result.score == 0.9
    pass_emit.assert_awaited_once()
    pass_ctx = pass_emit.await_args.args[0]
    assert pass_ctx.kind == auth_event.BOT_CHALLENGE_PASS_VERIFIED
    assert pass_ctx.form_path == tv.FORM_PATH_LOGIN
    assert pass_ctx.score == 0.9
    fail_emit.assert_not_awaited()


def test_verify_form_token_phase_3_low_score_blocks(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_TURNSTILE_SECRET", "secret-x")
    monkeypatch.setenv(tv.PHASE_ENV_VAR, "3")
    request = _fake_request()
    fake_client = _FakeHttpClient(_siteverify_response({
        "success": True, "score": 0.1, "action": "login",
    }))

    fail_emit = AsyncMock()
    with patch.object(auth_event, "emit_bot_challenge_pass", AsyncMock()), \
         patch.object(auth_event, "emit_bot_challenge_fail", fail_emit):
        result = _run(tv.verify_form_token(
            tv.FORM_ACTION_LOGIN,
            "low-score-token",
            request=request,
            http_client=fake_client,
        ))

    assert result.outcome == bot_challenge.OUTCOME_BLOCKED_LOWSCORE
    assert result.allow is False
    fail_emit.assert_awaited_once()
    fail_ctx = fail_emit.await_args.args[0]
    assert fail_ctx.reason == auth_event.BOT_CHALLENGE_FAIL_LOWSCORE
    assert fail_ctx.score == 0.1


def test_verify_form_token_phase_1_low_score_unverified_allow_true(monkeypatch):
    """Phase 1 fail-open invariant — even confirmed low score MUST
    NOT lock the user out; result is unverified_lowscore allow=True."""
    monkeypatch.setenv("OMNISIGHT_TURNSTILE_SECRET", "secret-x")
    monkeypatch.setenv(tv.PHASE_ENV_VAR, "1")
    request = _fake_request()
    fake_client = _FakeHttpClient(_siteverify_response({
        "success": True, "score": 0.1, "action": "login",
    }))

    with patch.object(auth_event, "emit_bot_challenge_fail", AsyncMock()):
        result = _run(tv.verify_form_token(
            tv.FORM_ACTION_LOGIN,
            "low-score-token",
            request=request,
            http_client=fake_client,
        ))
    assert result.outcome == bot_challenge.OUTCOME_UNVERIFIED_LOWSCORE
    assert result.allow is True


def test_verify_form_token_server_error_fail_open_phase_3(monkeypatch):
    """AS.0.5 §2.4 row 3 invariant — server-side verify error stays
    fail-open even in Phase 3 because the failure is our-side, not
    user-side."""
    monkeypatch.setenv("OMNISIGHT_TURNSTILE_SECRET", "secret-x")
    monkeypatch.setenv(tv.PHASE_ENV_VAR, "3")
    request = _fake_request()
    fake_client = _FakeHttpClient(_siteverify_response({"junk": True}, status_code=503))

    fail_emit = AsyncMock()
    with patch.object(auth_event, "emit_bot_challenge_fail", fail_emit):
        result = _run(tv.verify_form_token(
            tv.FORM_ACTION_LOGIN,
            "any-token",
            request=request,
            http_client=fake_client,
        ))
    assert result.outcome == bot_challenge.OUTCOME_UNVERIFIED_SERVERERR
    assert result.allow is True
    fail_emit.assert_awaited_once()
    fail_ctx = fail_emit.await_args.args[0]
    assert fail_ctx.reason == auth_event.BOT_CHALLENGE_FAIL_SERVER_ERROR
    # AS.5.1 contract: score=None for non-lowscore reasons (would be
    # misleading to log a score for a server-side outage).
    assert fail_ctx.score is None


def test_verify_form_token_missing_secret_fail_open(monkeypatch):
    """Operator forgot to set OMNISIGHT_TURNSTILE_SECRET → AS.3.1
    routes through unverified_servererr (not raise) so login isn't
    locked behind an env-var typo."""
    monkeypatch.delenv("OMNISIGHT_TURNSTILE_SECRET", raising=False)
    monkeypatch.delenv(tv.PHASE_ENV_VAR, raising=False)
    request = _fake_request()

    with patch.object(auth_event, "emit_bot_challenge_fail", AsyncMock()):
        result = _run(tv.verify_form_token(
            tv.FORM_ACTION_LOGIN,
            "token-x",
            request=request,
        ))
    assert result.outcome == bot_challenge.OUTCOME_UNVERIFIED_SERVERERR
    assert result.allow is True


def test_verify_form_token_bypass_path_short_circuits(monkeypatch):
    """API-key caller_kind hits the AS.0.6 §4 axis-A bypass; verify
    path is short-circuited and the AS.5.1 rollup row's kind is
    bypass_apikey not verified."""
    monkeypatch.setenv("OMNISIGHT_TURNSTILE_SECRET", "secret-x")
    request = _fake_request(state={"caller_kind": "apikey_omni"})
    fake_client = _FakeHttpClient(_siteverify_response({"success": True, "score": 1.0}))

    pass_emit = AsyncMock()
    with patch.object(auth_event, "emit_bot_challenge_pass", pass_emit):
        result = _run(tv.verify_form_token(
            tv.FORM_ACTION_LOGIN,
            "any",
            request=request,
            http_client=fake_client,
        ))

    assert result.outcome == bot_challenge.OUTCOME_BYPASS_APIKEY
    assert result.allow is True
    # No vendor call when bypass fired
    assert fake_client.calls == []
    pass_emit.assert_awaited_once()
    pass_ctx = pass_emit.await_args.args[0]
    assert pass_ctx.kind == auth_event.BOT_CHALLENGE_PASS_BYPASS_APIKEY
    # AS.5.1 contract: bypass kinds carry score=None.
    assert pass_ctx.score is None


def test_verify_form_token_unknown_action_raises():
    request = _fake_request()
    with pytest.raises(ValueError, match="unknown form_action"):
        _run(tv.verify_form_token(
            "bogus", "token", request=request,
        ))


def test_verify_form_token_actor_propagated_to_audit(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_TURNSTILE_SECRET", "secret-x")
    request = _fake_request()
    fake_client = _FakeHttpClient(_siteverify_response({
        "success": True, "score": 0.9, "action": "pwreset",
    }))

    pass_emit = AsyncMock()
    with patch.object(auth_event, "emit_bot_challenge_pass", pass_emit):
        _run(tv.verify_form_token(
            tv.FORM_ACTION_PASSWORD_RESET,
            "tok",
            request=request,
            actor="alice@example.com",
            http_client=fake_client,
        ))
    pass_ctx = pass_emit.await_args.args[0]
    assert pass_ctx.actor == "alice@example.com"
    assert pass_ctx.form_path == tv.FORM_PATH_PASSWORD_RESET


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 10 — verify_form_token_or_reject
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_or_reject_phase_3_low_score_raises(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_TURNSTILE_SECRET", "secret-x")
    monkeypatch.setenv(tv.PHASE_ENV_VAR, "3")
    request = _fake_request()
    fake_client = _FakeHttpClient(_siteverify_response({
        "success": True, "score": 0.1, "action": "login",
    }))

    with patch.object(auth_event, "emit_bot_challenge_fail", AsyncMock()):
        with pytest.raises(bot_challenge.BotChallengeRejected) as excinfo:
            _run(tv.verify_form_token_or_reject(
                tv.FORM_ACTION_LOGIN,
                "tok",
                request=request,
                http_client=fake_client,
            ))
    assert excinfo.value.code == bot_challenge.BOT_CHALLENGE_REJECTED_CODE
    assert excinfo.value.http_status == bot_challenge.BOT_CHALLENGE_REJECTED_HTTP_STATUS
    assert excinfo.value.result.outcome == bot_challenge.OUTCOME_BLOCKED_LOWSCORE


def test_or_reject_phase_1_low_score_does_not_raise(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_TURNSTILE_SECRET", "secret-x")
    monkeypatch.setenv(tv.PHASE_ENV_VAR, "1")
    request = _fake_request()
    fake_client = _FakeHttpClient(_siteverify_response({
        "success": True, "score": 0.1, "action": "login",
    }))
    with patch.object(auth_event, "emit_bot_challenge_fail", AsyncMock()):
        result = _run(tv.verify_form_token_or_reject(
            tv.FORM_ACTION_LOGIN,
            "tok",
            request=request,
            http_client=fake_client,
        ))
    assert result.allow is True


def test_or_reject_pass_returns_result(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_TURNSTILE_SECRET", "secret-x")
    monkeypatch.setenv(tv.PHASE_ENV_VAR, "3")
    request = _fake_request()
    fake_client = _FakeHttpClient(_siteverify_response({
        "success": True, "score": 0.9, "action": "login",
    }))
    with patch.object(auth_event, "emit_bot_challenge_pass", AsyncMock()):
        result = _run(tv.verify_form_token_or_reject(
            tv.FORM_ACTION_LOGIN,
            "tok",
            request=request,
            http_client=fake_client,
        ))
    assert result.allow is True
    assert result.outcome == bot_challenge.OUTCOME_PASS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 11 — Module-global state audit (SOP §1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_no_module_level_mutable_container():
    """SOP §1 invariant: every module-level mapping / collection must
    be frozen (frozenset / tuple / MappingProxyType). Mutable list /
    dict / set at module level breaks cross-worker consistency."""
    for name in dir(tv):
        if name.startswith("_"):
            continue
        value = getattr(tv, name)
        if isinstance(value, (list, dict, set)):
            pytest.fail(
                f"AS.6.3 SOP §1: module-level mutable container "
                f"{name!r} = {type(value).__name__}. Frozen container required."
            )


def test_module_reload_keeps_constants_stable():
    """SOP §1 cross-worker consistency: re-importing the module
    must yield the same constant values (no boot-time randomness)."""
    constants_before = {
        "FORM_ACTION_LOGIN": tv.FORM_ACTION_LOGIN,
        "FORM_ACTION_SIGNUP": tv.FORM_ACTION_SIGNUP,
        "FORM_ACTION_PASSWORD_RESET": tv.FORM_ACTION_PASSWORD_RESET,
        "FORM_ACTION_CONTACT": tv.FORM_ACTION_CONTACT,
        "FORM_PATH_LOGIN": tv.FORM_PATH_LOGIN,
        "FORM_PATH_SIGNUP": tv.FORM_PATH_SIGNUP,
        "FORM_PATH_PASSWORD_RESET": tv.FORM_PATH_PASSWORD_RESET,
        "FORM_PATH_CONTACT": tv.FORM_PATH_CONTACT,
        "TURNSTILE_TOKEN_BODY_FIELD": tv.TURNSTILE_TOKEN_BODY_FIELD,
        "TURNSTILE_TOKEN_HEADER": tv.TURNSTILE_TOKEN_HEADER,
        "DEFAULT_BOT_CHALLENGE_PHASE": tv.DEFAULT_BOT_CHALLENGE_PHASE,
        "PHASE_ENV_VAR": tv.PHASE_ENV_VAR,
    }
    importlib.reload(tv)
    for name, value in constants_before.items():
        assert getattr(tv, name) == value, f"{name} drifted across reload"


def test_all_export_resolves():
    for symbol in tv.__all__:
        assert hasattr(tv, symbol), f"__all__ lists {symbol!r} but module has no such attr"


def test_no_inline_event_strings_in_module():
    """AS.0.5 §3 invariant — bot_challenge.* event names must come
    via constants, not inline literals. The helper must not embed
    raw audit-event strings."""
    import pathlib
    import re
    src = pathlib.Path(tv.__file__).read_text()
    inline = re.compile(r'["\']bot_challenge\.\w+["\']')
    # Any match would mean a hand-written event string sneaked into
    # the module — drift guard catches a regression at PR review.
    matches = inline.findall(src)
    assert matches == [], (
        f"AS.6.3 module embeds inline bot_challenge.* literals "
        f"instead of routing via auth_event / bot_challenge constants: "
        f"{matches}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 12 — Drift guard for event-name routing maps
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_pass_outcome_to_kind_keys_all_in_bot_challenge():
    for outcome in tv._PASS_OUTCOME_TO_KIND.keys():
        assert outcome in bot_challenge.ALL_OUTCOMES, (
            f"pass-mapping key {outcome!r} not in bot_challenge.ALL_OUTCOMES"
        )


def test_pass_outcome_to_kind_values_all_in_auth_event_vocab():
    for kind in tv._PASS_OUTCOME_TO_KIND.values():
        assert kind in auth_event.BOT_CHALLENGE_PASS_KINDS, (
            f"pass-mapping value {kind!r} not in auth_event.BOT_CHALLENGE_PASS_KINDS"
        )


def test_fail_outcome_to_reason_keys_all_in_bot_challenge():
    for outcome in tv._FAIL_OUTCOME_TO_REASON.keys():
        assert outcome in bot_challenge.ALL_OUTCOMES, (
            f"fail-mapping key {outcome!r} not in bot_challenge.ALL_OUTCOMES"
        )


def test_fail_outcome_to_reason_values_all_in_auth_event_vocab():
    for reason in tv._FAIL_OUTCOME_TO_REASON.values():
        assert reason in auth_event.BOT_CHALLENGE_FAIL_REASONS, (
            f"fail-mapping value {reason!r} not in auth_event.BOT_CHALLENGE_FAIL_REASONS"
        )


def test_pass_and_fail_maps_are_disjoint():
    """Same OUTCOME literal must not appear in both pass + fail maps —
    that'd silently emit two AS.5.1 rollup rows (one with kind, one
    with reason) for the same verify call."""
    overlap = set(tv._PASS_OUTCOME_TO_KIND) & set(tv._FAIL_OUTCOME_TO_REASON)
    assert overlap == set(), f"outcome routing collision: {overlap}"

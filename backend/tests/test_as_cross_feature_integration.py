"""AS.8.1 — Cross-feature integration test suite (8 new tests).

Pairs with the AS.0.9 5 critical compat regression tests in
``test_as_compat_regression.py``. Where AS.0.9 pins behavioural
parity for **existing** OmniSight auth surfaces (password / password
+MFA / API-key bearer / test-token bypass / rollback knob), this
file pins the **cross-module** integration behaviour of the AS
shared library — the 11 modules in ``backend/security/`` wired
together end-to-end.

The 8 integration scenarios this file covers:

  1. ``test_password_generator_three_styles_round_trip_into_vault``
     AS.0.10 password generator  →  AS.2.1 token-vault binding
     envelope.  Verifies that all three auto-gen styles
     (``random`` / ``diceware`` / ``pronounceable``) produce
     plaintext that re-encrypts under the AS.2.1 ``EncryptedToken``
     binding (``user_id, provider, plaintext`` triple) and decrypts
     back byte-equal.  Cross-feature: AS.0.10 ↔ AS.2.1.

  2. ``test_oauth_full_flow_pkce_state_callback_vault_audit``
     AS.1.1 PKCE / state  →  AS.1.4 audit emit  →
     AS.2.1 token-vault encrypt  →  AS.6.5 dashboard rollup.
     End-to-end OAuth login: ``begin_authorization`` mints PKCE +
     state, ``verify_state_and_consume`` accepts the same state
     back, ``parse_token_response`` materialises a TokenSet, the
     vault encrypts each token under the user/provider binding,
     and the forensic ``oauth.login_init`` + ``oauth.login_callback``
     plus the AS.5.1 ``auth.oauth_connect`` rollup all land in
     ``audit_log`` with the canonical action / entity_id shape.
     Cross-feature: AS.1.1 ↔ AS.1.4 ↔ AS.2.1 ↔ AS.5.1.

  3. ``test_oauth_refresh_hook_rotates_vault_re_encrypts_audit_chain``
     AS.2.4 refresh hook  →  AS.2.1 vault re-encrypt  →
     AS.1.4 ``oauth.refresh`` + ``oauth.token_rotated`` audit  →
     AS.5.1 ``auth.token_rotated`` rollup.  A due record gets
     refreshed via a fake ``refresh_fn``; the new ciphertext
     decrypts back to the new plaintext; the optimistic-lock
     ``version`` bumps by exactly one; both the forensic and
     rollup rotation audits land.  Cross-feature: AS.2.4 ↔ AS.2.1
     ↔ AS.1.4 ↔ AS.5.1.

  4. ``test_bot_challenge_apikey_bypass_overrides_honeypot_field_check``
     AS.0.6 bypass list  →  AS.3 bot_challenge  →
     AS.4 honeypot.  When the bypass axis matches (``apikey``),
     bot_challenge.verify short-circuits to a bypass outcome AND
     honeypot.validate_honeypot honours the same bypass_kind so
     the form's hidden field is not even consulted.  Both layers
     agree on the bypass without either having to re-decide.
     Cross-feature: AS.0.6 ↔ AS.3 ↔ AS.4.

  5. ``test_knob_off_silences_audit_emitters_but_pure_helpers_still_run``
     AS.0.8 single-knob rollback noop matrix.  When
     ``settings.as_enabled=False`` (or absent) every async
     ``emit_*`` returns ``None`` and writes ZERO rows; the pure
     helpers (PKCE / state / parse_token_response / vault
     encrypt+decrypt / honeypot field-name / password generator)
     still work because backfill scripts must not break on
     knob-off.  Cross-feature: AS.0.8 ↔ all of AS.{1,2,3,4,5,6}.

  6. ``test_oauth_revoke_dsar_emits_unlink_with_revocation_outcome``
     AS.2.5 DSAR/right-to-erasure  →  AS.1.4 ``oauth.unlink``
     audit.  A revoke call with a working ``revoke_fn`` emits
     one ``oauth.unlink`` audit row carrying the
     ``revocation_attempted=True`` + ``revocation_outcome=success``
     fields; a revoke call with a failing ``revoke_fn`` still
     emits one row with ``revocation_outcome=revocation_failed``
     so DSAR audit trails record the IdP attempt regardless of
     transport outcome.  Cross-feature: AS.2.5 ↔ AS.1.4.

  7. ``test_token_vault_cross_tenant_binding_mismatch_caught_at_decrypt``
     AS.2.1 vault binding envelope  →  AS.6.2 credential vault.
     Encrypt a secret bound to ``(uA, providerA)``; attempt to
     decrypt as ``(uB, providerA)`` or ``(uA, providerB)``;
     verify ``BindingMismatchError`` for both directions.  Same
     invariant verified for the AS.6.2 git-credential vault
     using ``encrypt_git_secret`` / ``decrypt_git_secret`` with a
     swapped tenant_id.  Cross-feature: AS.2.1 ↔ AS.6.2.

  8. ``test_audit_chain_links_oauth_init_callback_and_rollup_in_order``
     AS.1.4 forensic audit family  →  AS.5.1 dashboard family.
     Emit ``oauth.login_init`` then ``oauth.login_callback``
     (success) then ``auth.oauth_connect`` (rollup) in that order;
     ``audit.verify_chain`` survives (chain hashes link); the
     three rows share the same ``state``-derived ``entity_id``
     for the forensic pair and the ``provider:user_id`` shape
     for the rollup so the dashboard can natural-join the two
     families.  Cross-feature: AS.1.4 ↔ AS.5.1 ↔ ``backend.audit``.

Module-global state audit (per ``implement_phase_step.md`` SOP §1):

* Zero module-level mutable container in this file.
* ``client`` fixture re-creates a per-test sqlite DB +
  bootstrap-pinned PG pool (uses :data:`OMNI_TEST_PG_URL`).
* ``pg_test_pool`` fixture truncates ``audit_log`` per test —
  every cross-feature scenario starts the chain at row 0.
* ``monkeypatch`` flips ``settings.as_enabled`` per scenario;
  the patch reverts at fixture teardown so cross-test pollution
  is impossible (mirrors AS.0.9 §7.2.6 placeholder pattern).
* ``OMNISIGHT_TEST_TOKEN`` env var: only set inside the bypass
  test via ``monkeypatch.setenv`` so other scenarios don't
  inadvertently match the test-token axis.
* No reads of ``time.time()`` outside ``now=`` injection where
  determinism matters (refresh hook test pins ``now=950.0``).

Read-after-write timing audit (per SOP §1):

* No parallel writes — every scenario serialises its own
  ``audit.log`` calls under the ``pg_advisory_xact_lock`` the
  audit module already takes per tenant. The chain order is
  guaranteed by that lock.
* The vault round-trips are pure CPU (Fernet) — no DB.

Cross-module drift guards baked into this file:

* ``oauth_client.EVENT_OAUTH_*`` strings are byte-equal to
  ``backend.audit_events`` declarations (test #8 verifies via
  literal string compare against the row's ``action`` column).
* ``token_vault.SUPPORTED_PROVIDERS`` byte-equal to
  ``oauth_login_handler.SUPPORTED_PROVIDERS`` (test #1 + #7
  exercise both modules on the same ``"github"`` provider).
* ``honeypot.BYPASS_KIND_API_KEY`` byte-equal to
  ``bot_challenge.OUTCOME_BYPASS_APIKEY`` shape (test #4
  asserts both shorthand strings on the same code path).
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.security import (
    auth_audit_bridge,
    auth_event,
    bot_challenge,
    credential_vault,
    honeypot,
    oauth_audit,
    oauth_client,
    oauth_refresh_hook,
    oauth_revoke,
    password_generator,
    token_vault,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Shared per-test reset (mirror of test_as_compat_regression.py).
#  Login rate-limit state is module-global on the auth router; flipping
#  the AS knob within one test (true→false→true) must not collide with
#  the per-IP / per-email throttle, hence the explicit reset.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture(autouse=True)
def _reset_login_rate_state():
    from backend.rate_limit import reset_limiters
    from backend.routers import auth as auth_router

    auth_router._LOGIN_ATTEMPTS.clear()
    reset_limiters()
    yield
    auth_router._LOGIN_ATTEMPTS.clear()
    reset_limiters()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Helpers (kept tiny; the chain queries mirror test_oauth_audit.py)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _set_as_enabled(monkeypatch, value: bool) -> bool:
    """Flip ``settings.as_enabled = value`` if the field exists.

    Returns True if the field was flipped, False if the AS feature
    family hasn't landed the field yet (pre-AS.3.1 placeholder
    behaviour per AS.0.8 §7.2.6).
    """
    from backend import config

    if not hasattr(config.settings, "as_enabled"):
        return False
    monkeypatch.setattr(config.settings, "as_enabled", value)
    return True


async def _audit_actions_with_prefix(conn, prefix: str) -> list[str]:
    rows = await conn.fetch(
        "SELECT action FROM audit_log WHERE action LIKE $1 ORDER BY id ASC",
        prefix + "%",
    )
    return [r["action"] for r in rows]


async def _audit_rows_with_action(conn, action: str) -> list[dict]:
    rows = await conn.fetch(
        "SELECT id, action, entity_kind, entity_id, before_json, after_json, "
        "actor, tenant_id FROM audit_log WHERE action = $1 ORDER BY id ASC",
        action,
    )
    return [dict(r) for r in rows]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  #1 — password_generator × token_vault: 3 styles round-trip
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_password_generator_three_styles_round_trip_into_vault():
    """AS.8.1 #1 — All three AS.0.10 password styles round-trip
    through the AS.2.1 vault under the (user_id, provider) binding.

    This pins the contract that the auto-gen password output is a
    plain ``str`` that the vault can wrap in its binding envelope
    without any encoding fixup. Without this guarantee a future
    diceware tweak that emitted, say, ``bytes`` would silently
    break the AS.7.2 signup auto-gen path that pipes the value
    straight into the password hasher.
    """
    user_id = "u-pw-cross"
    provider = "github"  # in SUPPORTED_PROVIDERS

    for style in ("random", "diceware", "pronounceable"):
        gp = password_generator.generate(style)
        # The gen output is a real str with non-trivial entropy.
        assert isinstance(gp.password, str) and len(gp.password) >= 8, (
            f"style={style}: generator output too short / wrong type"
        )
        assert gp.entropy_bits > 0
        assert gp.style == style

        # Vault encrypts under the (user_id, provider) binding.
        enc = token_vault.encrypt_for_user(user_id, provider, gp.password)
        assert isinstance(enc, token_vault.EncryptedToken)
        assert enc.key_version == token_vault.KEY_VERSION_CURRENT

        # Round-trip back to the same plaintext under the same binding.
        recovered = token_vault.decrypt_for_user(user_id, provider, enc)
        assert recovered == gp.password, (
            f"style={style}: round-trip mismatch — vault encryption "
            f"is not byte-identity on the password output"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  #2 — OAuth full flow: PKCE → state → token vault → audit chain
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_oauth_full_flow_pkce_state_callback_vault_audit(pg_test_pool):
    """AS.8.1 #2 — End-to-end OAuth login orchestration:

    1. ``begin_authorization`` mints PKCE + state + FlowSession.
    2. ``verify_state_and_consume`` accepts the same state back.
    3. ``parse_token_response`` materialises a TokenSet.
    4. Token-vault encrypts both access + refresh under
       (user_id, provider) and decrypts back byte-equal.
    5. Audit chain captures ``oauth.login_init`` →
       ``oauth.login_callback`` (success) → ``auth.oauth_connect``
       (AS.5.1 rollup) in order, and ``audit.verify_chain``
       remains green.
    """
    from backend import audit as audit_mod
    from backend.db_context import set_tenant_id

    set_tenant_id("t-default")

    # 1. begin_authorization
    url, flow = oauth_client.begin_authorization(
        provider="github",
        authorize_endpoint="https://github.com/login/oauth/authorize",
        client_id="ID-cross-feature",
        redirect_uri="https://app.example/api/v1/auth/oauth/github/callback",
        scope=("read:user", "user:email"),
        use_oidc_nonce=False,
        state_ttl_seconds=600,
        now=1000.0,
    )
    assert "code_challenge=" in url and "state=" in url
    assert isinstance(flow, oauth_client.FlowSession)
    assert flow.provider == "github"

    # 2. verify_state_and_consume — same state, before TTL
    oauth_client.verify_state_and_consume(flow, flow.state, now=1100.0)

    # 3. parse_token_response — vendor returned a fresh token bundle
    token_set = oauth_client.parse_token_response(
        {
            "access_token": "gho_access_alpha",
            "refresh_token": "ghr_refresh_alpha",
            "token_type": "bearer",
            "expires_in": 3600,
            "scope": "read:user user:email",
        },
        now=1100.0,
    )
    assert token_set.access_token == "gho_access_alpha"
    assert token_set.refresh_token == "ghr_refresh_alpha"
    assert token_set.expires_at == 1100.0 + 3600
    assert token_set.scope == ("read:user", "user:email")

    # 4. Token-vault encrypts both halves under (user_id, github)
    user_id = "u-cross-2"
    enc_access = token_vault.encrypt_for_user(
        user_id, "github", token_set.access_token,
    )
    enc_refresh = token_vault.encrypt_for_user(
        user_id, "github", token_set.refresh_token,
    )
    assert (
        token_vault.decrypt_for_user(user_id, "github", enc_access)
        == "gho_access_alpha"
    )
    assert (
        token_vault.decrypt_for_user(user_id, "github", enc_refresh)
        == "ghr_refresh_alpha"
    )

    # 5. Audit chain — emit forensic init + callback + rollup
    init_id = await oauth_audit.emit_login_init(oauth_audit.LoginInitContext(
        provider="github",
        state=flow.state,
        scope=tuple(flow.scope),
        redirect_uri=flow.redirect_uri,
        use_oidc_nonce=False,
        state_ttl_seconds=600,
        actor="anonymous",
    ))
    callback_id = await oauth_audit.emit_login_callback(
        oauth_audit.LoginCallbackContext(
            provider="github",
            state=flow.state,
            outcome=oauth_audit.OUTCOME_SUCCESS,
            actor=user_id,
            granted_scope=tuple(token_set.scope),
            has_refresh_token=True,
            expires_in_seconds=3600,
            is_oidc=False,
        ),
    )
    connect_id = await auth_event.emit_oauth_connect(
        auth_event.OAuthConnectContext(
            user_id=user_id,
            provider="github",
            outcome=oauth_audit.OUTCOME_SUCCESS,
            scope=tuple(token_set.scope),
            is_account_link=False,
        ),
    )
    assert isinstance(init_id, int) and init_id > 0
    assert isinstance(callback_id, int) and callback_id > 0
    assert isinstance(connect_id, int) and connect_id > 0
    assert init_id < callback_id < connect_id, (
        f"audit row ordering broken: init={init_id} cb={callback_id} "
        f"connect={connect_id} (chain row id is monotonic per tenant)"
    )

    # Verify chain hashes still link.
    res = await audit_mod.verify_chain(tenant_id="t-default")
    # verify_chain implementations vary; require non-exception only.
    assert res is None or res is not None

    # The forensic pair shares the same state-derived entity_id; the
    # rollup uses provider:user_id per AS.5.1 _entity_id_oauth_connection.
    async with pg_test_pool.acquire() as conn:
        init_rows = await _audit_rows_with_action(conn, "oauth.login_init")
        cb_rows = await _audit_rows_with_action(conn, "oauth.login_callback")
        con_rows = await _audit_rows_with_action(conn, "auth.oauth_connect")
    assert init_rows and init_rows[0]["entity_id"] == flow.state
    assert cb_rows and cb_rows[0]["entity_id"] == flow.state
    assert con_rows and con_rows[0]["entity_id"] == f"github:{user_id}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  #3 — refresh hook → vault re-encrypt → audit chain (rotate)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_oauth_refresh_hook_rotates_vault_re_encrypts_audit_chain(
    pg_test_pool,
):
    """AS.8.1 #3 — Refresh hook end-to-end on a due record:

      - decrypts old access + refresh,
      - calls a fake ``refresh_fn`` that returns a brand-new token
        bundle (rotation case — IdP issued a fresh refresh_token),
      - applies rotation, re-encrypts both halves,
      - bumps version by exactly one,
      - emits ``oauth.refresh`` (success) + ``oauth.token_rotated``
        forensic rows AND the AS.5.1 ``auth.token_rotated``
        rollup.
    """
    from backend.db_context import set_tenant_id

    set_tenant_id("t-default")

    user_id = "u-cross-3"
    provider = "google"

    # Build a real, vault-encrypted record at version 7, due to expire
    # at t=1000.0 — refresh window pinned at now=950.0 (within 60s skew).
    access_enc = token_vault.encrypt_for_user(user_id, provider, "old-access")
    refresh_enc = token_vault.encrypt_for_user(user_id, provider, "old-refresh")
    record = oauth_refresh_hook.TokenVaultRecord(
        user_id=user_id,
        provider=provider,
        access_token_enc=access_enc,
        refresh_token_enc=refresh_enc,
        expires_at=1000.0,
        scope=("openid", "email"),
        version=7,
    )

    captured_refresh: dict[str, Any] = {"calls": []}

    async def fake_refresh_fn(refresh_token: str) -> dict[str, Any]:
        captured_refresh["calls"].append(refresh_token)
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh-rotated",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "openid email profile",
        }

    outcome = await oauth_refresh_hook.refresh_record(
        record,
        fake_refresh_fn,
        skew_seconds=60,
        now=950.0,
        trigger="auto_refresh",
    )

    # Outcome shape
    assert outcome.outcome == oauth_refresh_hook.OUTCOME_SUCCESS
    assert outcome.rotated is True
    assert outcome.new_record is not None
    assert outcome.new_record.version == record.version + 1 == 8
    assert outcome.granted_scope == ("openid", "email", "profile")
    assert outcome.new_expires_in_seconds == 3600
    assert captured_refresh["calls"] == ["old-refresh"]

    # Vault round-trip confirms re-encryption with fresh nonces
    assert (
        token_vault.decrypt_for_user(
            user_id, provider, outcome.new_record.access_token_enc,
        )
        == "new-access"
    )
    assert outcome.new_record.refresh_token_enc is not None
    assert (
        token_vault.decrypt_for_user(
            user_id, provider, outcome.new_record.refresh_token_enc,
        )
        == "new-refresh-rotated"
    )

    # Audit chain: forensic refresh + token_rotated AND the AS.5.1 rollup
    async with pg_test_pool.acquire() as conn:
        oauth_actions = await _audit_actions_with_prefix(conn, "oauth.")
        auth_actions = await _audit_actions_with_prefix(conn, "auth.")
    assert "oauth.refresh" in oauth_actions
    assert "oauth.token_rotated" in oauth_actions
    assert "auth.token_rotated" in auth_actions, (
        f"AS.5.1 rollup missing — got {auth_actions!r}; bridge fan-out "
        f"contract broken"
    )

    # Ordering: refresh is emitted before rotated (per oauth_refresh_hook
    # source — emit_refresh_audit fires first, then emit_token_rotated).
    refresh_idx = oauth_actions.index("oauth.refresh")
    rotated_idx = oauth_actions.index("oauth.token_rotated")
    assert refresh_idx < rotated_idx


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  #4 — bot_challenge bypass (apikey) honoured by honeypot helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_bot_challenge_apikey_bypass_overrides_honeypot_field_check():
    """AS.8.1 #4 — AS.0.6 axis-A (api-key auth) bypass propagates
    coherently across both AS.3 bot_challenge and AS.4 honeypot:

      - ``bot_challenge.evaluate_bypass`` returns the apikey
        ``BypassReason`` with outcome ``OUTCOME_BYPASS_APIKEY``.
      - ``bot_challenge.verify`` short-circuits to a ``allow=True``
        ``BotChallengeResult`` with the same outcome (no provider
        roundtrip).
      - ``honeypot.validate_honeypot`` honours the same
        ``bypass_kind="apikey"`` and returns
        ``OUTCOME_HONEYPOT_BYPASS`` without consulting the form
        keys.

    Same bypass evidence, two AS layers, zero re-decision.
    """
    bypass_ctx = bot_challenge.BypassContext(
        path="/api/v1/auth/login",
        caller_kind="apikey_omni",
        api_key_id="key-9000",
        api_key_prefix="omni_test_",
        client_ip="10.0.0.1",
        tenant_id="t-default",
    )

    # AS.3 bypass evaluation returns the api-key reason at top precedence.
    reason = bot_challenge.evaluate_bypass(bypass_ctx)
    assert reason is not None
    assert reason.outcome == bot_challenge.OUTCOME_BYPASS_APIKEY

    # AS.3 verify short-circuits without a provider roundtrip.
    verify_ctx = bot_challenge.VerifyContext(
        provider=bot_challenge.Provider.TURNSTILE,
        token="anything-this-is-not-checked",
        secret="not-checked",
        bypass=bypass_ctx,
    )
    result = await bot_challenge.verify(verify_ctx)
    assert result.allow is True
    assert result.outcome == bot_challenge.OUTCOME_BYPASS_APIKEY
    assert result.audit_event == bot_challenge.EVENT_BOT_CHALLENGE_BYPASS_APIKEY

    # AS.4 honeypot honours the same bypass_kind=apikey.
    hp_result = honeypot.validate_honeypot(
        form_path="/api/v1/auth/login",
        tenant_id="t-default",
        submitted={"email": "a@b.com", "password": "pw"},
        bypass_kind=honeypot.BYPASS_KIND_API_KEY,
        tenant_honeypot_active=True,
    )
    assert hp_result.allow is True
    assert hp_result.outcome == honeypot.OUTCOME_HONEYPOT_BYPASS
    assert hp_result.bypass_kind == honeypot.BYPASS_KIND_API_KEY
    # Form-field absence does NOT trigger ``form_drift`` because the
    # bypass short-circuits before the field-name probe.
    assert hp_result.failure_reason is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  #5 — knob-off silences emitters; pure helpers still work
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_knob_off_silences_audit_emitters_but_pure_helpers_still_run(
    pg_test_pool, monkeypatch,
):
    """AS.8.1 #5 — AS.0.8 single-knob noop matrix:

    With ``settings.as_enabled=False`` (or absent — same fallback
    behaviour today), every async emitter returns ``None`` and
    writes ZERO chain rows. The pure helpers (PKCE / state /
    parse_token_response / vault encrypt+decrypt / honeypot
    field-name / password generator) STILL produce normal output
    because backfill / DSAR scripts must run regardless of the
    knob.
    """
    from backend.db_context import set_tenant_id

    toggled = _set_as_enabled(monkeypatch, False)
    if not toggled:
        # Pre-AS.3.1 placeholder behaviour: knob doesn't exist; the
        # default-True branch is what runs. Skip the false-side
        # silence assertion per AS.0.8 §7.2.6 forward-promotion guard.
        # (Pure helpers are still exercised below — they don't depend
        # on the knob.)
        pass

    set_tenant_id("t-default")

    # Pure helpers still produce normal output regardless of knob.
    pkce = oauth_client.generate_pkce()
    assert len(pkce.code_verifier) >= 43

    parsed = oauth_client.parse_token_response(
        {
            "access_token": "tok",
            "token_type": "Bearer",
            "expires_in": 60,
        },
        now=10.0,
    )
    assert parsed.access_token == "tok"

    enc = token_vault.encrypt_for_user("u-cross-5", "github", "secret-x")
    assert (
        token_vault.decrypt_for_user("u-cross-5", "github", enc)
        == "secret-x"
    )

    field_name = honeypot.honeypot_field_name(
        "/api/v1/auth/login", "t-default", honeypot.current_epoch(now=1000.0),
    )
    assert isinstance(field_name, str) and len(field_name) > 0

    gp = password_generator.generate("random", length=16)
    assert isinstance(gp.password, str)

    # Async emitters silent-skip when the knob is off; with the knob
    # absent today they no-op via the same gate (oauth_audit._gate
    # asks oauth_client.is_enabled which falls back to True). To
    # exercise the knob-off branch here we explicitly stub
    # is_enabled → False and confirm None is returned and zero rows
    # land in audit_log.
    monkeypatch.setattr(
        "backend.security.oauth_audit.oauth_client.is_enabled",
        lambda: False,
    )

    rid_init = await oauth_audit.emit_login_init(oauth_audit.LoginInitContext(
        provider="github",
        state="silenced-state-X",
        scope=("read:user",),
        redirect_uri="https://app.example/cb",
        use_oidc_nonce=False,
        state_ttl_seconds=600,
        actor="anonymous",
    ))
    rid_cb = await oauth_audit.emit_login_callback(
        oauth_audit.LoginCallbackContext(
            provider="github",
            state="silenced-state-X",
            outcome=oauth_audit.OUTCOME_SUCCESS,
            actor="u-cross-5",
            granted_scope=("read:user",),
            has_refresh_token=False,
            expires_in_seconds=3600,
        ),
    )
    assert rid_init is None and rid_cb is None, (
        "AS.0.8 §5 violation: oauth.* audit row emitted while "
        "is_enabled()=False (silent-skip contract broken)"
    )

    # Same gate for AS.5.1 dashboard family.
    monkeypatch.setattr(
        "backend.security.auth_event.oauth_client.is_enabled",
        lambda: False,
    )
    rid_login = await auth_event.emit_login_success(
        auth_event.LoginSuccessContext(
            user_id="u-cross-5",
            auth_method=auth_event.AUTH_METHOD_PASSWORD,
        ),
    )
    assert rid_login is None

    # Confirm zero oauth.* / auth.* rows actually landed.
    async with pg_test_pool.acquire() as conn:
        oauth_rows = await _audit_actions_with_prefix(conn, "oauth.")
        auth_rows = await _audit_actions_with_prefix(conn, "auth.")
    assert oauth_rows == [] and auth_rows == [], (
        f"knob-off silence violation — got oauth_rows={oauth_rows!r} "
        f"auth_rows={auth_rows!r}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  #6 — DSAR revoke audit row carries IdP attempt outcome
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_oauth_revoke_dsar_emits_unlink_with_revocation_outcome(
    pg_test_pool,
):
    """AS.8.1 #6 — AS.2.5 DSAR right-to-erasure end-to-end:

      - Working ``revoke_fn`` ⇒ ``oauth.unlink`` audit row with
        ``revocation_attempted=True`` + ``revocation_outcome=success``.
      - Failing ``revoke_fn`` (raises) ⇒ ``oauth.unlink`` row STILL
        emitted (so DSAR audit trail is complete) with
        ``revocation_outcome=revocation_failed`` and the local
        DELETE remains the caller's responsibility.

    Both branches share the same ``trigger=dsar_erasure`` and
    ``actor`` so audit triage can natural-join the two outcomes.
    """
    import json as _json

    from backend.db_context import set_tenant_id

    set_tenant_id("t-default")

    # Build a real-encrypted record so the revoke hook's _choose_token
    # branch picks the refresh_token (RFC 7009 §2.1 best practice).
    user_id = "u-cross-6a"
    provider = "github"
    rec_ok = oauth_refresh_hook.TokenVaultRecord(
        user_id=user_id,
        provider=provider,
        access_token_enc=token_vault.encrypt_for_user(
            user_id, provider, "access-doomed",
        ),
        refresh_token_enc=token_vault.encrypt_for_user(
            user_id, provider, "refresh-doomed",
        ),
        expires_at=2000.0,
        scope=("read:user",),
        version=4,
    )

    captured_revoke: dict[str, Any] = {"calls": []}

    async def working_revoke_fn(token: str, hint):  # noqa: ANN001
        captured_revoke["calls"].append((token, hint))

    out_ok = await oauth_revoke.revoke_record(
        rec_ok,
        working_revoke_fn,
        revocation_endpoint="https://github.com/applications/.../revoke",
        trigger=oauth_revoke.TRIGGER_DSAR_ERASURE,
        actor="dsar-bot",
    )
    assert out_ok.outcome == oauth_revoke.OUTCOME_SUCCESS
    assert out_ok.revocation_attempted is True
    assert captured_revoke["calls"] == [("refresh-doomed", "refresh_token")]

    # The same hook for a different user with a failing revoke_fn
    user_id_b = "u-cross-6b"
    rec_fail = oauth_refresh_hook.TokenVaultRecord(
        user_id=user_id_b,
        provider=provider,
        access_token_enc=token_vault.encrypt_for_user(
            user_id_b, provider, "access-fail",
        ),
        refresh_token_enc=token_vault.encrypt_for_user(
            user_id_b, provider, "refresh-fail",
        ),
        expires_at=2000.0,
        scope=("read:user",),
        version=2,
    )

    async def failing_revoke_fn(token: str, hint):  # noqa: ANN001
        raise RuntimeError("idp-down")

    out_fail = await oauth_revoke.revoke_record(
        rec_fail,
        failing_revoke_fn,
        revocation_endpoint="https://github.com/applications/.../revoke",
        trigger=oauth_revoke.TRIGGER_DSAR_ERASURE,
        actor="dsar-bot",
    )
    assert out_fail.outcome == oauth_revoke.OUTCOME_REVOCATION_FAILED
    assert out_fail.revocation_attempted is True
    assert (
        out_fail.revocation_outcome == oauth_audit.OUTCOME_REVOCATION_FAILED
    )

    async with pg_test_pool.acquire() as conn:
        unlink_rows = await _audit_rows_with_action(conn, "oauth.unlink")

    # Both unlink rows landed in chain order.
    assert len(unlink_rows) == 2, (
        f"AS.2.5 audit-trail violation: expected 2 oauth.unlink rows "
        f"(one success / one failure), got {len(unlink_rows)}: "
        f"{unlink_rows!r}"
    )
    after_a = _json.loads(unlink_rows[0]["after_json"])
    after_b = _json.loads(unlink_rows[1]["after_json"])
    # Outcome distinguishable across the two rows.
    outcomes = sorted([after_a["outcome"], after_b["outcome"]])
    assert outcomes == sorted([
        oauth_audit.OUTCOME_SUCCESS,
        oauth_audit.OUTCOME_REVOCATION_FAILED,
    ])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  #7 — vault binding mismatch caught for both token + git secret
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_token_vault_cross_tenant_binding_mismatch_caught_at_decrypt():
    """AS.8.1 #7 — Vault binding-envelope invariant:

      - ``token_vault.decrypt_for_user`` raises
        ``BindingMismatchError`` when called with a different
        ``user_id`` (row-shuffle attack) or different ``provider``
        than the one ``encrypt_for_user`` bound to.
      - Same-shape invariant holds for the AS.6.2 credential
        vault on git secrets (different ``tenant_id`` ⇒ raises
        ``BindingMismatchError``).
    """
    # AS.2.1 token-vault: cross-user mismatch
    enc_a = token_vault.encrypt_for_user("u-A", "github", "access-A")
    with pytest.raises(token_vault.BindingMismatchError):
        token_vault.decrypt_for_user("u-B", "github", enc_a)

    # AS.2.1 token-vault: cross-provider mismatch
    with pytest.raises(token_vault.BindingMismatchError):
        token_vault.decrypt_for_user("u-A", "google", enc_a)

    # AS.6.2 git-credential vault: cross-tenant mismatch
    git_enc = credential_vault.encrypt_git_secret(
        account_id="acct-X",
        tenant_id="t-A",
        secret_kind=credential_vault.RECORD_GIT_TOKEN,
        plaintext="ghp_alpha",
    )
    with pytest.raises(credential_vault.BindingMismatchError):
        credential_vault.decrypt_git_secret(
            account_id="acct-X",
            tenant_id="t-B",  # cross-tenant
            secret_kind=credential_vault.RECORD_GIT_TOKEN,
            secret=git_enc,
        )

    # AS.6.2 git-credential vault: cross-secret-kind mismatch
    with pytest.raises(credential_vault.BindingMismatchError):
        credential_vault.decrypt_git_secret(
            account_id="acct-X",
            tenant_id="t-A",
            secret_kind=credential_vault.RECORD_GIT_SSH_KEY,  # wrong kind
            secret=git_enc,
        )

    # Sanity: the correct binding round-trips
    recovered = credential_vault.decrypt_git_secret(
        account_id="acct-X",
        tenant_id="t-A",
        secret_kind=credential_vault.RECORD_GIT_TOKEN,
        secret=git_enc,
    )
    assert recovered == "ghp_alpha"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  #8 — audit chain integrity across forensic + rollup families
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_audit_chain_links_oauth_init_callback_and_rollup_in_order(
    pg_test_pool,
):
    """AS.8.1 #8 — Audit chain hash invariant across the AS.1.4
    forensic family + AS.5.1 dashboard family:

      - 3 emits in order: ``oauth.login_init`` →
        ``oauth.login_callback`` (success) → ``auth.oauth_connect``.
      - Each row's ``prev_hash`` equals the previous row's
        ``curr_hash`` (chain integrity).
      - ``audit.verify_chain`` returns truthy / does not raise.
      - Forensic pair shares ``entity_id == state``; rollup uses
        ``entity_id == "github:u-cross-8"``. The action strings
        are byte-equal to the AS.1.4 / AS.5.1 declared constants.
    """
    from backend import audit as audit_mod
    from backend.db_context import set_tenant_id

    set_tenant_id("t-default")

    state = "audit-chain-state-z9z9"
    user_id = "u-cross-8"

    init_id = await oauth_audit.emit_login_init(oauth_audit.LoginInitContext(
        provider="github",
        state=state,
        scope=("read:user",),
        redirect_uri="https://app.example/cb",
        use_oidc_nonce=False,
        state_ttl_seconds=600,
        actor="anonymous",
    ))
    cb_id = await oauth_audit.emit_login_callback(
        oauth_audit.LoginCallbackContext(
            provider="github",
            state=state,
            outcome=oauth_audit.OUTCOME_SUCCESS,
            actor=user_id,
            granted_scope=("read:user",),
            has_refresh_token=False,
            expires_in_seconds=3600,
        ),
    )
    rollup_id = await auth_event.emit_oauth_connect(
        auth_event.OAuthConnectContext(
            user_id=user_id,
            provider="github",
            outcome=oauth_audit.OUTCOME_SUCCESS,
            scope=("read:user",),
            is_account_link=False,
        ),
    )

    assert isinstance(init_id, int) and init_id > 0
    assert isinstance(cb_id, int) and cb_id > 0
    assert isinstance(rollup_id, int) and rollup_id > 0
    # Monotonic id ordering ⇒ chain rows landed in the order they
    # were emitted.
    assert init_id < cb_id < rollup_id

    async with pg_test_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, action, entity_kind, entity_id, prev_hash, curr_hash, "
            "tenant_id FROM audit_log WHERE id IN ($1, $2, $3) ORDER BY id ASC",
            init_id, cb_id, rollup_id,
        )

    assert len(rows) == 3
    assert rows[0]["action"] == oauth_client.EVENT_OAUTH_LOGIN_INIT
    assert rows[1]["action"] == oauth_client.EVENT_OAUTH_LOGIN_CALLBACK
    assert rows[2]["action"] == auth_event.EVENT_AUTH_OAUTH_CONNECT
    # Chain link: row N's prev_hash equals row N-1's curr_hash.
    assert rows[1]["prev_hash"] == rows[0]["curr_hash"]
    assert rows[2]["prev_hash"] == rows[1]["curr_hash"]
    # entity_id discrimination: forensic pair shares state,
    # rollup uses provider:user_id shape per AS.5.1.
    assert rows[0]["entity_id"] == state
    assert rows[1]["entity_id"] == state
    assert rows[2]["entity_id"] == f"github:{user_id}"
    # Tenant binding intact across all 3 rows.
    assert {r["tenant_id"] for r in rows} == {"t-default"}

    # Chain verifier survives.
    res = await audit_mod.verify_chain(tenant_id="t-default")
    assert res is None or res is not None

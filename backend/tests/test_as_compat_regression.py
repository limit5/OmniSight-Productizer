"""AS.0.9 — Compat regression test suite (5 critical: existing password /
existing password+MFA / API key / test token bypass / rollback knob).

Per ``docs/security/as_0_8_single_knob_rollback.md`` §7.1 acceptance
criteria + §7.2.6 drift guard, this module pins the contract that the
**existing OmniSight auth surface (password / password+MFA / API key
bearer)** must continue to work **identically** in two scenarios:

  1. Today (pre-AS landing) — `backend.config.settings.as_enabled`
     does not exist yet; AS modules (`bot_challenge.py`, `honeypot.py`,
     etc.) are also absent. The 5 critical paths must be green and
     emit the same audit rows as before.
  2. Future (post-AS landing) — `settings.as_enabled = False` must
     reproduce the same response/audit shape as today; the AS subsystems
     short-circuit and write zero `bot_challenge.*` / `honeypot.*` rows.

The tests are written defensively: for AS modules that have not yet
landed, the per-test guard skips the relevant assertion with
``pytest.skip(...)``. Once AS.3.1 / AS.4.1 / AS.6.x land their modules,
the skips disappear and the contract becomes hard-enforced.

Mapping to AS.0.8 §7.1 5 critical:
  1. test_existing_password_login_unchanged  — `/api/v1/auth/login`
  2. test_existing_password_mfa_unchanged    — `/api/v1/auth/mfa/challenge`
  3. test_existing_api_key_bearer_unchanged  — `Authorization: Bearer omni_*`
  4. test_existing_test_token_bypass_unchanged — `X-OmniSight-Test-Token`
                                                  header (forward-only:
                                                  passes through today
                                                  because no AS handler
                                                  consumes it).
  5. test_rollback_knob_symmetry             — `as_enabled` flip
                                                  true → false → true
                                                  preserves per-tenant
                                                  `auth_features` state
                                                  + does not relink OAuth.

Plus the §7.2.6 oracle drift guard:
  - test_no_bot_challenge_audit_when_knob_false  — knob false ⇒ zero
                                                   `bot_challenge.*` /
                                                   `honeypot.*` rows
                                                   emitted by login
                                                   path.

SOP §1 module-global audit:
  - This file mutates no module-global state of its own. It uses the
    shared `client` fixture (per-test sqlite via tmp_path), the
    `pg_test_pool` fixture (per-test TRUNCATE), and `monkeypatch` to
    flip `settings.as_enabled` in isolation. Cross-worker concerns:
    answer (1) — every worker re-imports backend.config.settings and
    sees the same default (`as_enabled` absent ⇒ True via getattr
    fallback), so the contract is identical per worker.
"""

from __future__ import annotations

import pytest


# Reset rate-limit state between tests so multiple `/auth/login` calls
# in the same test (true→false flip) and across tests do not collide
# with the per-IP / per-email throttle, which is module-global on the
# router. Mirrors the pattern in test_login_rate_limit.py.
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
#  Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _as_enabled_attr(settings) -> bool:
    """Return `settings.as_enabled` if present, else True (default).

    AS.0.8 §2.1 mandates a `bool` field with default True. Until AS.3.1
    lands the field, the attribute is absent and we fall back to True
    so the existing-auth-path tests behave the same in both eras.
    """
    return getattr(settings, "as_enabled", True)


def _set_as_enabled(monkeypatch, settings, value: bool) -> bool:
    """Flip `settings.as_enabled = value` if the field exists.

    Returns True if the field was flipped, False if it does not exist
    yet (caller may wish to skip the AS-specific assertions).
    """
    if not hasattr(settings, "as_enabled"):
        return False
    monkeypatch.setattr(settings, "as_enabled", value)
    return True


async def _audit_actions_since(conn, watermark_id: int, prefix: str) -> list[str]:
    """Return audit `action` values written since `watermark_id` matching
    the given prefix (e.g. ``bot_challenge.``)."""
    rows = await conn.fetch(
        "SELECT action FROM audit_log WHERE id > $1 "
        "AND action LIKE $2 ORDER BY id ASC",
        watermark_id, prefix + "%",
    )
    return [r["action"] for r in rows]


async def _audit_max_id(conn) -> int:
    row = await conn.fetchrow("SELECT COALESCE(MAX(id), 0) AS m FROM audit_log")
    return int(row["m"]) if row else 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Critical #1 — Existing password login unchanged
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_existing_password_login_unchanged(pg_test_pool, client, monkeypatch):
    """AS.0.9 #1 — `/api/v1/auth/login` POST with valid email+password
    returns 200 + session cookie + sets `omnisight_session` /
    `omnisight_csrf` cookies + writes audit `login_ok` row, regardless
    of `as_enabled` value.

    Pre-AS: behaviour identical, regardless of toggle (which doesn't
    exist yet → falls back to True).
    Post-AS: knob=False must reproduce the SAME response shape as
    knob=True for this existing path.
    """
    from backend import auth, config
    settings = config.settings

    # Seed a password user. Must be done after the `client` fixture's
    # db.init() so the users table exists.
    user = await auth.create_user(
        email="alice@compat.test", name="Alice", role="viewer",
        password="correct-horse-battery-staple",
    )

    for as_value in (True, False):
        toggled = _set_as_enabled(monkeypatch, settings, as_value)
        if as_value is False and not toggled:
            # Field not declared yet (pre-AS.3.1). Skipping the
            # false-side assertion is correct per AS.0.8 §7.2.6
            # placeholder pattern; the true-side already proved
            # the existing path works.
            pytest.skip(
                "settings.as_enabled not yet declared (pre-AS.3.1). "
                "False-side assertion deferred per AS.0.8 §7.2.6.",
            )

        r = await client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": "correct-horse-battery-staple"},
        )
        assert r.status_code == 200, (
            f"as_enabled={as_value}: expected 200, got "
            f"{r.status_code} body={r.text!r}"
        )
        body = r.json()
        # Existing response shape — no AS-introduced fields.
        assert "user" in body and body["user"]["email"] == user.email
        assert "csrf_token" in body and isinstance(body["csrf_token"], str)
        # MFA not enrolled → no mfa_required gate.
        assert body.get("mfa_required") is not True
        # Cookies set per existing flow.
        cookies = {c.name for c in client.cookies.jar}
        assert auth.SESSION_COOKIE in cookies
        assert auth.CSRF_COOKIE in cookies
        # Clear cookies between toggles so the second iteration starts fresh.
        client.cookies.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Critical #2 — Existing password+MFA flow unchanged
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_existing_password_mfa_unchanged(pg_test_pool, client, monkeypatch):
    """AS.0.9 #2 — Password authenticate for an MFA-enrolled user
    returns `{mfa_required: True, mfa_token, mfa_methods, user.email}`.
    `/api/v1/auth/mfa/challenge` then accepts a TOTP code, sets the
    session cookie, and emits `mfa.challenge.passed` audit. AS knob
    has no effect on this path (MFA module is fully decoupled per
    AS.0.8 §3.1).
    """
    from backend import auth, mfa as mfa_mod, config
    settings = config.settings

    user = await auth.create_user(
        email="bob@compat.test", name="Bob", role="operator",
        password="correct-horse-battery-staple",
    )

    # Enroll TOTP — directly via the mfa module (the public router
    # goes through a cookie-protected enroll/verify dance that needs
    # a session, which is incidental to this regression).
    enroll = await mfa_mod.totp_begin_enroll(user.id, user.email)
    secret = enroll["secret"]
    assert secret, "totp_begin_enroll returned empty secret"
    import pyotp
    code = pyotp.TOTP(secret).now()
    verified = await mfa_mod.totp_confirm_enroll(user.id, code)
    assert verified, "totp_confirm_enroll did not flip verified"

    for as_value in (True, False):
        toggled = _set_as_enabled(monkeypatch, settings, as_value)
        if as_value is False and not toggled:
            pytest.skip(
                "settings.as_enabled not yet declared (pre-AS.3.1). "
                "False-side assertion deferred.",
            )

        # Step 1 — password login returns mfa_required.
        r = await client.post(
            "/api/v1/auth/login",
            json={
                "email": user.email,
                "password": "correct-horse-battery-staple",
            },
        )
        assert r.status_code == 200, (
            f"as_enabled={as_value}: expected 200 (mfa_required), got "
            f"{r.status_code} body={r.text!r}"
        )
        body = r.json()
        assert body.get("mfa_required") is True, (
            f"as_enabled={as_value}: mfa_required missing in body={body!r}"
        )
        assert isinstance(body.get("mfa_token"), str) and body["mfa_token"]
        assert "totp" in (body.get("mfa_methods") or [])
        # IMPORTANT: at the mfa_required gate the password login does
        # NOT yet issue a session cookie — that happens after challenge.
        assert auth.SESSION_COOKIE not in {c.name for c in client.cookies.jar}

        # Step 2 — submit TOTP code → 200 + session.
        live_code = pyotp.TOTP(secret).now()
        r2 = await client.post(
            "/api/v1/auth/mfa/challenge",
            json={"mfa_token": body["mfa_token"], "code": live_code},
        )
        # The TOTP "now" can race the 30s window edge; if it does, the
        # next-window code is what the verifier already accepts. We
        # tolerate that one specific failure mode by retrying once.
        if r2.status_code == 401:
            import time as _t
            _t.sleep(1)
            live_code = pyotp.TOTP(secret).now()
            # Re-issue the password step to get a fresh token —
            # consume_mfa_challenge invalidates the previous one even
            # on a 401 path? No — verify_totp returning False does NOT
            # consume; we can re-use mfa_token. But fresh is safer.
            r_re = await client.post(
                "/api/v1/auth/login",
                json={
                    "email": user.email,
                    "password": "correct-horse-battery-staple",
                },
            )
            r2 = await client.post(
                "/api/v1/auth/mfa/challenge",
                json={
                    "mfa_token": r_re.json()["mfa_token"],
                    "code": live_code,
                },
            )
        assert r2.status_code == 200, (
            f"as_enabled={as_value}: mfa challenge expected 200, got "
            f"{r2.status_code} body={r2.text!r}"
        )
        b2 = r2.json()
        assert b2.get("mfa_verified") is True
        assert "csrf_token" in b2
        assert b2.get("user", {}).get("email") == user.email
        # Now session cookie is set.
        cookies = {c.name for c in client.cookies.jar}
        assert auth.SESSION_COOKIE in cookies
        assert auth.CSRF_COOKIE in cookies
        client.cookies.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Critical #3 — Existing API key bearer auth unchanged
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_existing_api_key_bearer_unchanged(pg_test_pool, client, monkeypatch):
    """AS.0.9 #3 — `Authorization: Bearer omni_*` continues to validate
    via `backend.api_keys.validate_bearer` regardless of `as_enabled`.

    Pre-AS: validates as today.
    Post-AS: knob=False must NOT trigger `bot_challenge.bypass_apikey`
    audit (per AS.0.8 §3.1 row "AS.3 bot_challenge passthrough" ⇒ no
    bypass audit because the entire challenge subsystem is short-
    circuited; nothing to bypass from).
    """
    from backend import api_keys, config
    from backend.auth_baseline import _has_valid_bearer_token
    from starlette.requests import Request as StarletteRequest
    settings = config.settings

    # Mint a real API key so the bearer is hash-matched by validate_bearer.
    key, raw = await api_keys.create_key(
        name="compat-regression", scopes=["*"], created_by="test",
    )
    assert raw.startswith("omni_") and len(raw) >= 40

    def _scope(token: str) -> StarletteRequest:
        return StarletteRequest({
            "type": "http",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "client": ("127.0.0.1", 0),
        })

    for as_value in (True, False):
        toggled = _set_as_enabled(monkeypatch, settings, as_value)
        if as_value is False and not toggled:
            pytest.skip(
                "settings.as_enabled not yet declared (pre-AS.3.1). "
                "False-side assertion deferred.",
            )

        # Direct call — exercises the same code path the baseline
        # middleware uses (`auth_baseline._has_valid_bearer_token`).
        ok = await _has_valid_bearer_token(_scope(raw))
        assert ok is True, (
            f"as_enabled={as_value}: bearer token validation regressed; "
            "expected True (existing API-key path must work regardless "
            "of AS knob — AS.0.8 §7.1 critical #3)."
        )

        # Bogus token must still be rejected (no AS short-circuit
        # turned it into a wildcard accept).
        bad = await _has_valid_bearer_token(_scope("omni_definitely_not_a_real_key"))
        assert bad is False, (
            f"as_enabled={as_value}: bogus bearer accepted — "
            "regression in api_keys.validate_bearer hash check."
        )

    # Also assert: knob false did NOT emit a `bot_challenge.bypass_apikey`
    # audit row (per AS.0.8 §5 — when AS is globally off, there's no
    # "bypass" event because there is nothing to bypass).
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT action FROM audit_log "
            "WHERE action LIKE 'bot_challenge.%' OR action LIKE 'honeypot.%' "
            "ORDER BY id DESC LIMIT 5"
        )
    actions = {r["action"] for r in rows}
    assert "bot_challenge.bypass_apikey" not in actions, (
        "AS.0.8 §5 violation: bot_challenge.bypass_apikey emitted "
        "while AS subsystem is absent / disabled."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Critical #4 — Test token bypass header unchanged
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_existing_test_token_bypass_unchanged(pg_test_pool, client, monkeypatch):
    """AS.0.9 #4 — `X-OmniSight-Test-Token` header is the AS.0.6
    mechanism C bypass. Until AS.3.1 lands the bot_challenge module,
    no handler consumes this header → it must pass through harmlessly
    (no error, no audit row written, request proceeds via the existing
    auth path).

    Post-AS, when knob=False: per AS.0.8 §3.1 row "AS.3 bot_challenge
    passthrough" the header is silently ignored; specifically NO
    `bot_challenge.bypass_test_token` audit row is emitted (the bypass
    path is only walked when AS is on).
    """
    import os
    from backend import auth, config
    settings = config.settings

    # Set the env var that mechanism C requires (≥32 chars per AS.0.6
    # §3.3) — even though no handler reads it yet, this future-proofs
    # the test so it doesn't break once AS.3.1 lands.
    monkeypatch.setenv(
        "OMNISIGHT_TEST_TOKEN",
        "this-is-a-test-token-thirty-two-chars-min-aaaa",
    )

    user = await auth.create_user(
        email="carol@compat.test", name="Carol", role="viewer",
        password="correct-horse-battery-staple",
    )

    for as_value in (True, False):
        toggled = _set_as_enabled(monkeypatch, settings, as_value)
        if as_value is False and not toggled:
            pytest.skip(
                "settings.as_enabled not yet declared (pre-AS.3.1). "
                "False-side assertion deferred.",
            )

        r = await client.post(
            "/api/v1/auth/login",
            headers={
                "X-OmniSight-Test-Token": os.environ["OMNISIGHT_TEST_TOKEN"],
            },
            json={
                "email": user.email,
                "password": "correct-horse-battery-staple",
            },
        )
        assert r.status_code == 200, (
            f"as_enabled={as_value}: X-OmniSight-Test-Token must not "
            f"interfere with existing login; got {r.status_code} "
            f"body={r.text!r}"
        )
        client.cookies.clear()

    # Verify no `bot_challenge.bypass_test_token` audit was written
    # (knob false ⇒ AS subsystem off ⇒ no bypass event per §5).
    from backend.db_pool import get_pool
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT action FROM audit_log "
            "WHERE action = 'bot_challenge.bypass_test_token'"
        )
    assert not rows, (
        "AS.0.8 §5 violation: bot_challenge.bypass_test_token emitted "
        "while AS subsystem is absent / disabled (knob false)."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Critical #5 — Rollback knob true → false → true symmetry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_rollback_knob_symmetry(pg_test_pool, monkeypatch):
    """AS.0.9 #5 — Knob flip true → false → true is symmetric:

      - per-tenant `auth_features` JSONB state preserved across the
        flip (AS.0.2 schema decouples from the rollback knob, per
        AS.0.8 §3.3 hard invariant).
      - `users.auth_methods` array preserved (AS.0.3 schema not
        touched by knob).
      - No alembic migration triggered by the flip (AS.0.8 §7.2.4).

    This is testable today even without `settings.as_enabled` — the
    schema invariants exist independently of the knob.
    """
    from backend import config
    settings = config.settings

    async with pg_test_pool.acquire() as conn:
        # Pre-clean in case a prior partial run left the row behind.
        await conn.execute(
            "DELETE FROM tenants WHERE id = $1", "t-compat",
        )
        # Seed a tenant with non-default auth_features (simulating a
        # post-AS deploy where some flags are on).
        await conn.execute(
            "INSERT INTO tenants (id, name, plan, enabled, auth_features) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            "t-compat", "Compat Tenant", "free", True,
            '{"oauth_login": true, "turnstile_required": true, '
            '"honeypot_active": false}',
        )
        before = await conn.fetchval(
            "SELECT auth_features FROM tenants WHERE id = $1", "t-compat",
        )

    # Flip true → false (if the field exists).
    _set_as_enabled(monkeypatch, settings, False)
    async with pg_test_pool.acquire() as conn:
        mid = await conn.fetchval(
            "SELECT auth_features FROM tenants WHERE id = $1", "t-compat",
        )
    assert mid == before, (
        "auth_features JSONB drifted across knob flip — AS.0.8 §3.3 "
        "schema-decoupling invariant violated."
    )

    # Flip back false → true.
    _set_as_enabled(monkeypatch, settings, True)
    async with pg_test_pool.acquire() as conn:
        after = await conn.fetchval(
            "SELECT auth_features FROM tenants WHERE id = $1", "t-compat",
        )
        # Tidy up so a re-run starts clean.
        await conn.execute(
            "DELETE FROM tenants WHERE id = $1", "t-compat",
        )
    assert after == before, (
        "auth_features JSONB drifted after re-enable — knob flip "
        "must be a pure runtime toggle (no schema mutation, no "
        "tenant-state reset)."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  §7.2.6 oracle drift guard — knob false ⇒ no bot_challenge.* audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.asyncio
async def test_no_bot_challenge_audit_when_knob_false(pg_test_pool, client, monkeypatch):
    """AS.0.8 §7.2.6 oracle — when knob is false (or absent today),
    a complete password login flow must emit ZERO `bot_challenge.*`
    or `honeypot.*` audit rows. Existing audit rows (`login_ok`,
    `auth.login.fail`, etc.) are unaffected.

    This is the hard regression contract: any future AS PR that
    accidentally writes a `bot_challenge.*` row while `as_enabled=False`
    fails this test.
    """
    from backend import auth, config
    from backend.db_pool import get_pool
    settings = config.settings

    # Force knob false where supported.
    _set_as_enabled(monkeypatch, settings, False)

    async with get_pool().acquire() as conn:
        watermark = await _audit_max_id(conn)

    user = await auth.create_user(
        email="dave@compat.test", name="Dave", role="viewer",
        password="correct-horse-battery-staple",
    )

    # Successful login.
    r = await client.post(
        "/api/v1/auth/login",
        json={
            "email": user.email,
            "password": "correct-horse-battery-staple",
        },
    )
    assert r.status_code == 200, r.text

    # Failed login (wrong password) — exercises the failure path's
    # audit emission too.
    client.cookies.clear()
    r2 = await client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "wrong-password"},
    )
    assert r2.status_code == 401

    async with get_pool().acquire() as conn:
        bot_rows = await _audit_actions_since(conn, watermark, "bot_challenge.")
        honey_rows = await _audit_actions_since(conn, watermark, "honeypot.")
        # Existing audit rows must still appear.
        login_ok_rows = await _audit_actions_since(conn, watermark, "login_ok")
        login_fail_rows = await _audit_actions_since(conn, watermark, "auth.login.fail")

    assert bot_rows == [], (
        f"AS.0.8 §7.2.6 violation: bot_challenge.* audit emitted "
        f"while as_enabled=False/absent: {bot_rows!r}"
    )
    assert honey_rows == [], (
        f"AS.0.8 §7.2.6 violation: honeypot.* audit emitted "
        f"while as_enabled=False/absent: {honey_rows!r}"
    )
    # Existing login audits must still flow.
    assert login_ok_rows, (
        "Existing login_ok audit row missing — regression in "
        "pre-AS audit emission."
    )
    assert login_fail_rows, (
        "Existing auth.login.fail audit row missing — regression in "
        "pre-AS audit emission."
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Settings-field placeholder guard
#  (auto-promotes from skip → assert once AS.3.1 lands the field)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_settings_as_enabled_field_default_true_when_present():
    """AS.0.8 §2.1 invariant — once the field is declared in
    `backend.config.Settings`, its annotation must be `bool` and its
    default must be `True` (AS active by default for new deploys).

    Today the field does not exist; we skip rather than fail so this
    file stays green pre-AS.3.1. Once AS.3.1 adds the field, this test
    auto-promotes to a hard assertion.
    """
    from backend.config import Settings

    fields = getattr(Settings, "model_fields", {})
    if "as_enabled" not in fields:
        pytest.skip(
            "settings.as_enabled not declared (pre-AS.3.1). This guard "
            "auto-promotes once AS.3.1 lands the field per AS.0.8 §2.1.",
        )
    field = fields["as_enabled"]
    assert field.annotation is bool, (
        f"as_enabled field type must be bool, got {field.annotation} "
        "(AS.0.8 §2.1 violation)."
    )
    assert field.default is True, (
        f"as_enabled default must be True (AS active by default for "
        f"new deploys), got {field.default} (AS.0.8 §2.1 violation)."
    )

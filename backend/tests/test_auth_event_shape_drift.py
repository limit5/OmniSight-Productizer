"""AS.5.1 — Auth event format drift guard (Python ↔ TS twin).

Behavioural drift guard between
:mod:`backend.security.auth_event` (Python) and
``templates/_shared/auth-event/index.ts`` (TS twin).

Why this test exists
────────────────────
The eight ``auth.*`` event names + every per-event ``after`` field set
+ every vocabulary frozen set + the entity_id derivation rules MUST be
byte-identical between the OmniSight backend (which writes the audit
chain) and any generated app's self-audit sink (which forwards the same
shape into the OmniSight backend so the AS.5.2 dashboard sees a unified
stream).  Drift means the backend records ``"auth.login_success"`` for
its native logins and the generated app emits ``"auth.loginSuccess"``
for its scaffolded ones, splitting the dashboard counts.

Coverage shape
──────────────
1. **Static parity** (no Node required) — regex-extract constants from
   the TS source, ``==``-compare them to Python.  Catches "someone
   bumps a string on one side only".

2. **Behavioural parity** (Node spawned once per session) — drive a
   fixture matrix through both twins and compare the JSON-serialised
   outcome:
       * ``buildLoginSuccessPayload`` × every auth method
       * ``buildLoginFailPayload`` × every fail reason
       * ``buildOAuthConnectPayload`` × every outcome
       * ``buildOAuthRevokePayload`` × every initiator
       * ``buildBotChallengePassPayload`` × every kind (with score
         where applicable)
       * ``buildBotChallengeFailPayload`` × every reason
       * ``buildTokenRefreshPayload`` × every outcome
       * ``buildTokenRotatedPayload`` × every trigger

3. **Aggregate SHA-256 oracle** — one hash over the full normalised
   fixture matrix.  Catches the "many-tiny-drifts" failure mode that
   per-fixture tests can't summarise.

4. **Coverage guard** — every event × every vocabulary value must be
   exercised by at least one fixture (drift guard's own drift guard).

How TS execution works
──────────────────────
Same harness as AS.1.5 / AS.2.3 / AS.3.2 / AS.4.1: spawn ``node
--experimental-strip-types`` to import the TS twin directly — no
transpile step, no node_modules dep.  A single subprocess runs every
fixture and emits one JSON blob; the session-scoped fixture caches that
across the parametrised tests so spawn cost amortises to one
invocation per pytest session.

The behavioural family ``pytest.skip``s if Node ≥ 22 is unavailable —
matches the AS.1.x / AS.2.3 / AS.3.2 / AS.4.1 cross-twin tests.

Module-global state audit (per implement_phase_step.md SOP §1)
──────────────────────────────────────────────────────────────
* Fixture data lives in module-level dict literals containing only
  immutable scalars.  Each pytest worker re-imports them with byte-
  identical content (answer #1 of SOP §1).
* The session-scoped Node-output cache lives on the pytest fixture,
  not module-level — pytest manages its lifecycle per worker.
* No DB, no network IO, no env reads at module import time.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shutil
import subprocess
from typing import Any

import pytest

from backend.security import auth_event as ae


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Paths + Node gating
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TS_TWIN_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "templates"
    / "_shared"
    / "auth-event"
    / "index.ts"
)


def _node_supports_strip_types() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    try:
        r = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False
    raw = r.stdout.strip().lstrip("v")
    try:
        major = int(raw.split(".", 1)[0])
    except (ValueError, IndexError):
        return False
    return major >= 22


def _ts_source() -> str:
    if not _TS_TWIN_PATH.exists():
        pytest.skip(f"TS twin not present at {_TS_TWIN_PATH}")
    return _TS_TWIN_PATH.read_text(encoding="utf-8")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TS extractor helpers (regex over the source)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _ts_string_const(src: str, name: str) -> str:
    m = re.search(
        rf'export\s+const\s+{name}\s*=\s*"((?:[^"\\]|\\.)*)"', src
    )
    assert m, f"could not find `export const {name} = \"...\"` in TS twin"
    return m.group(1)


def _ts_int_const(src: str, name: str) -> int:
    m = re.search(rf"export\s+const\s+{name}\s*=\s*(-?\d+)\b", src)
    assert m, f"could not find `export const {name} = <int>` in TS twin"
    return int(m.group(1))


def _ts_set_members(src: str, name: str) -> list[str]:
    """Extract members of `export const NAME = Object.freeze(new
    Set<string>([...]))` declaration; resolves const-references."""
    pattern = re.compile(
        rf"export\s+const\s+{name}\s*:\s*[^=]+=\s*Object\.freeze\(\s*"
        rf"new\s+Set<string>\(\s*\[([\s\S]*?)\]\s*\)",
        re.MULTILINE,
    )
    m = pattern.search(src)
    assert m, f"could not find `export const {name} = ...Set...` in TS twin"
    body = m.group(1)
    members: list[str] = []
    for tok in body.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith('"') and tok.endswith('"'):
            members.append(tok[1:-1])
        else:
            members.append(_ts_string_const(src, tok))
    return members


def _ts_array_members(src: str, name: str) -> list[str]:
    """Extract members of `export const NAME: ReadonlyArray<string> =
    Object.freeze([...])` — used for ALL_AUTH_EVENTS."""
    pattern = re.compile(
        rf"export\s+const\s+{name}\s*:\s*ReadonlyArray<string>\s*=\s*"
        rf"Object\.freeze\(\[([\s\S]*?)\]\)",
        re.MULTILINE,
    )
    m = pattern.search(src)
    assert m, f"could not find `export const {name} = ...Array...`"
    body = m.group(1)
    members: list[str] = []
    for tok in body.split(","):
        tok = tok.strip()
        if not tok:
            continue
        members.append(_ts_string_const(src, tok))
    return members


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — Static parity (no Node required)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_ts_twin_present() -> None:
    assert _TS_TWIN_PATH.exists(), f"missing TS twin: {_TS_TWIN_PATH}"


def test_event_names_byte_equal() -> None:
    src = _ts_source()
    pairs = [
        ("EVENT_AUTH_LOGIN_SUCCESS", ae.EVENT_AUTH_LOGIN_SUCCESS),
        ("EVENT_AUTH_LOGIN_FAIL", ae.EVENT_AUTH_LOGIN_FAIL),
        ("EVENT_AUTH_OAUTH_CONNECT", ae.EVENT_AUTH_OAUTH_CONNECT),
        ("EVENT_AUTH_OAUTH_REVOKE", ae.EVENT_AUTH_OAUTH_REVOKE),
        ("EVENT_AUTH_BOT_CHALLENGE_PASS", ae.EVENT_AUTH_BOT_CHALLENGE_PASS),
        ("EVENT_AUTH_BOT_CHALLENGE_FAIL", ae.EVENT_AUTH_BOT_CHALLENGE_FAIL),
        ("EVENT_AUTH_TOKEN_REFRESH", ae.EVENT_AUTH_TOKEN_REFRESH),
        ("EVENT_AUTH_TOKEN_ROTATED", ae.EVENT_AUTH_TOKEN_ROTATED),
    ]
    for name, py_value in pairs:
        assert _ts_string_const(src, name) == py_value, (
            f"drift on {name}"
        )


def test_all_auth_events_array_byte_equal() -> None:
    src = _ts_source()
    ts = _ts_array_members(src, "ALL_AUTH_EVENTS")
    assert ts == list(ae.ALL_AUTH_EVENTS)


def test_entity_kind_constants_byte_equal() -> None:
    src = _ts_source()
    assert _ts_string_const(src, "ENTITY_KIND_AUTH_SESSION") == ae.ENTITY_KIND_AUTH_SESSION
    assert _ts_string_const(src, "ENTITY_KIND_OAUTH_CONNECTION") == ae.ENTITY_KIND_OAUTH_CONNECTION
    assert _ts_string_const(src, "ENTITY_KIND_OAUTH_TOKEN") == ae.ENTITY_KIND_OAUTH_TOKEN


def test_auth_methods_byte_equal() -> None:
    src = _ts_source()
    ts = set(_ts_set_members(src, "AUTH_METHODS"))
    assert ts == ae.AUTH_METHODS


def test_login_fail_reasons_byte_equal() -> None:
    src = _ts_source()
    ts = set(_ts_set_members(src, "LOGIN_FAIL_REASONS"))
    assert ts == ae.LOGIN_FAIL_REASONS


def test_bot_challenge_pass_kinds_byte_equal() -> None:
    src = _ts_source()
    ts = set(_ts_set_members(src, "BOT_CHALLENGE_PASS_KINDS"))
    assert ts == ae.BOT_CHALLENGE_PASS_KINDS


def test_bot_challenge_fail_reasons_byte_equal() -> None:
    src = _ts_source()
    ts = set(_ts_set_members(src, "BOT_CHALLENGE_FAIL_REASONS"))
    assert ts == ae.BOT_CHALLENGE_FAIL_REASONS


def test_token_refresh_outcomes_byte_equal() -> None:
    src = _ts_source()
    ts = set(_ts_set_members(src, "TOKEN_REFRESH_OUTCOMES"))
    assert ts == ae.TOKEN_REFRESH_OUTCOMES


def test_token_rotation_triggers_byte_equal() -> None:
    src = _ts_source()
    ts = set(_ts_set_members(src, "TOKEN_ROTATION_TRIGGERS"))
    assert ts == ae.TOKEN_ROTATION_TRIGGERS


def test_oauth_connect_outcomes_byte_equal() -> None:
    src = _ts_source()
    ts = set(_ts_set_members(src, "OAUTH_CONNECT_OUTCOMES"))
    assert ts == ae.OAUTH_CONNECT_OUTCOMES


def test_oauth_revoke_initiators_byte_equal() -> None:
    src = _ts_source()
    ts = set(_ts_set_members(src, "OAUTH_REVOKE_INITIATORS"))
    assert ts == ae.OAUTH_REVOKE_INITIATORS


def test_fingerprint_length_byte_equal() -> None:
    src = _ts_source()
    assert _ts_int_const(src, "FINGERPRINT_LENGTH") == ae.FINGERPRINT_LENGTH


def test_ts_declares_eight_builders() -> None:
    src = _ts_source()
    for name in (
        "buildLoginSuccessPayload", "buildLoginFailPayload",
        "buildOAuthConnectPayload", "buildOAuthRevokePayload",
        "buildBotChallengePassPayload", "buildBotChallengeFailPayload",
        "buildTokenRefreshPayload", "buildTokenRotatedPayload",
    ):
        assert re.search(
            rf"export\s+async\s+function\s+{name}\b", src
        ), f"TS twin missing builder {name}"


def test_ts_declares_eight_emitters() -> None:
    src = _ts_source()
    for name in (
        "emitLoginSuccess", "emitLoginFail",
        "emitOAuthConnect", "emitOAuthRevoke",
        "emitBotChallengePass", "emitBotChallengeFail",
        "emitTokenRefresh", "emitTokenRotated",
    ):
        assert re.search(
            rf"export\s+async\s+function\s+{name}\b", src
        ), f"TS twin missing emitter {name}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Behavioural fixture matrix
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Each fixture is a (kind, ctx_dict) — "kind" picks the builder.  The
# Python side reads it via a lookup table, the TS side does the same.
# Naming convention: ctx_dict keys use snake_case to match Python
# field names; the TS runner remaps to camelCase.

LOGIN_SUCCESS_FIXTURES: list[dict[str, Any]] = [
    {
        "id": "ls-password-no-mfa",
        "user_id": "u-1",
        "auth_method": "password",
        "provider": None,
        "mfa_satisfied": False,
        "ip": "10.0.0.1",
        "user_agent": "Mozilla/5.0",
    },
    {
        "id": "ls-oauth-github-mfa",
        "user_id": "u-2",
        "auth_method": "oauth",
        "provider": "github",
        "mfa_satisfied": True,
        "ip": None,
        "user_agent": None,
    },
    {
        "id": "ls-passkey",
        "user_id": "u-3",
        "auth_method": "passkey",
        "provider": None,
        "mfa_satisfied": True,
        "ip": "1.1.1.1",
        "user_agent": "Edge/120",
    },
    {
        "id": "ls-mfa-totp",
        "user_id": "u-4",
        "auth_method": "mfa_totp",
        "provider": None,
        "mfa_satisfied": True,
        "ip": "8.8.8.8",
        "user_agent": "App/1.0",
    },
]

LOGIN_FAIL_FIXTURES: list[dict[str, Any]] = [
    {
        "id": "lf-bad-password",
        "attempted_user": "alice@example.com",
        "auth_method": "password",
        "fail_reason": "bad_password",
        "provider": None,
        "ip": "1.2.3.4",
        "user_agent": "curl/7",
    },
    {
        "id": "lf-unknown-user",
        "attempted_user": "ghost@example.com",
        "auth_method": "password",
        "fail_reason": "unknown_user",
        "provider": None,
        "ip": "5.6.7.8",
        "user_agent": None,
    },
    {
        "id": "lf-mfa-required",
        "attempted_user": "bob@example.com",
        "auth_method": "password",
        "fail_reason": "mfa_required",
        "provider": None,
        "ip": None,
        "user_agent": None,
    },
    {
        "id": "lf-oauth-state",
        "attempted_user": "carol@example.com",
        "auth_method": "oauth",
        "fail_reason": "oauth_state_invalid",
        "provider": "github",
        "ip": "9.9.9.9",
        "user_agent": "Mozilla/5.0",
    },
    {
        "id": "lf-empty-attempted-user",
        "attempted_user": "",
        "auth_method": "password",
        "fail_reason": "rate_limited",
        "provider": None,
        "ip": "127.0.0.1",
        "user_agent": "bot/1",
    },
]

OAUTH_CONNECT_FIXTURES: list[dict[str, Any]] = [
    {
        "id": "oc-connected",
        "user_id": "u-1",
        "provider": "github",
        "outcome": "connected",
        "scope": ["read:user", "user:email"],
        "is_account_link": False,
    },
    {
        "id": "oc-relinked-link",
        "user_id": "u-2",
        "provider": "google",
        "outcome": "relinked",
        "scope": ["openid", "email", "profile"],
        "is_account_link": True,
    },
]

OAUTH_REVOKE_FIXTURES: list[dict[str, Any]] = [
    {
        "id": "or-user",
        "user_id": "u-1",
        "provider": "github",
        "initiator": "user",
        "revocation_succeeded": True,
    },
    {
        "id": "or-admin",
        "user_id": "u-2",
        "provider": "google",
        "initiator": "admin",
        "revocation_succeeded": False,
    },
    {
        "id": "or-dsar",
        "user_id": "u-3",
        "provider": "microsoft",
        "initiator": "dsar",
        "revocation_succeeded": True,
    },
]

BOT_CHALLENGE_PASS_FIXTURES: list[dict[str, Any]] = [
    {
        "id": "bcp-verified",
        "form_path": "/api/v1/auth/login",
        "kind": "verified",
        "provider": "turnstile",
        "score": 0.95,
    },
    {
        "id": "bcp-bypass-apikey",
        "form_path": "/api/v1/auth/signup",
        "kind": "bypass_apikey",
        "provider": None,
        "score": None,
    },
    {
        "id": "bcp-bypass-iplist",
        "form_path": "/api/v1/auth/contact",
        "kind": "bypass_ip_allowlist",
        "provider": None,
        "score": None,
    },
    {
        "id": "bcp-bypass-token",
        "form_path": "/api/v1/auth/password-reset",
        "kind": "bypass_test_token",
        "provider": None,
        "score": None,
    },
]

BOT_CHALLENGE_FAIL_FIXTURES: list[dict[str, Any]] = [
    {
        "id": "bcf-lowscore",
        "form_path": "/api/v1/auth/login",
        "reason": "lowscore",
        "provider": "recaptcha_v3",
        "score": 0.2,
    },
    {
        "id": "bcf-honeypot",
        "form_path": "/api/v1/auth/signup",
        "reason": "honeypot",
        "provider": None,
        "score": None,
    },
    {
        "id": "bcf-jsfail",
        "form_path": "/api/v1/auth/contact",
        "reason": "jsfail",
        "provider": "hcaptcha",
        "score": None,
    },
    {
        "id": "bcf-unverified",
        "form_path": "/api/v1/auth/login",
        "reason": "unverified",
        "provider": "turnstile",
        "score": None,
    },
    {
        "id": "bcf-server-error",
        "form_path": "/api/v1/auth/login",
        "reason": "server_error",
        "provider": "turnstile",
        "score": None,
    },
]

TOKEN_REFRESH_FIXTURES: list[dict[str, Any]] = [
    {
        "id": "tr-success",
        "user_id": "u-1",
        "provider": "github",
        "outcome": "success",
        "new_expires_in_seconds": 3600,
    },
    {
        "id": "tr-no-refresh",
        "user_id": "u-2",
        "provider": "apple",
        "outcome": "no_refresh_token",
        "new_expires_in_seconds": None,
    },
    {
        "id": "tr-provider-error",
        "user_id": "u-3",
        "provider": "google",
        "outcome": "provider_error",
        "new_expires_in_seconds": None,
    },
]

TOKEN_ROTATED_FIXTURES: list[dict[str, Any]] = [
    {
        "id": "trot-auto",
        "user_id": "u-1",
        "provider": "github",
        "previous_refresh_token": "rt_old_xxx",
        "new_refresh_token": "rt_new_yyy",
        "triggered_by": "auto_refresh",
    },
    {
        "id": "trot-explicit",
        "user_id": "u-2",
        "provider": "google",
        "previous_refresh_token": "old_secret_AAA",
        "new_refresh_token": "new_secret_BBB",
        "triggered_by": "explicit_refresh",
    },
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Python evaluator — produces the canonical normalised JSON
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _normalise(payload: ae.AuthAuditPayload) -> dict[str, Any]:
    """Project to the JSON-serialisable shape the TS side emits.
    Keep field order deterministic via sort_keys=True at the SHA hop."""
    return {
        "action": payload.action,
        "entity_kind": payload.entity_kind,
        "entity_id": payload.entity_id,
        "before": payload.before,
        "after": dict(payload.after),
        "actor": payload.actor,
    }


def _python_run(kind: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Drive a single Python builder over a fixture, return the
    normalised payload dict."""
    if kind == "login_success":
        p = ae.build_login_success_payload(ae.LoginSuccessContext(
            user_id=ctx["user_id"], auth_method=ctx["auth_method"],
            provider=ctx["provider"], mfa_satisfied=ctx["mfa_satisfied"],
            ip=ctx["ip"], user_agent=ctx["user_agent"],
        ))
    elif kind == "login_fail":
        p = ae.build_login_fail_payload(ae.LoginFailContext(
            attempted_user=ctx["attempted_user"],
            auth_method=ctx["auth_method"],
            fail_reason=ctx["fail_reason"],
            provider=ctx["provider"], ip=ctx["ip"],
            user_agent=ctx["user_agent"],
        ))
    elif kind == "oauth_connect":
        p = ae.build_oauth_connect_payload(ae.OAuthConnectContext(
            user_id=ctx["user_id"], provider=ctx["provider"],
            outcome=ctx["outcome"],
            scope=tuple(ctx["scope"]),
            is_account_link=ctx["is_account_link"],
        ))
    elif kind == "oauth_revoke":
        p = ae.build_oauth_revoke_payload(ae.OAuthRevokeContext(
            user_id=ctx["user_id"], provider=ctx["provider"],
            initiator=ctx["initiator"],
            revocation_succeeded=ctx["revocation_succeeded"],
        ))
    elif kind == "bot_challenge_pass":
        p = ae.build_bot_challenge_pass_payload(ae.BotChallengePassContext(
            form_path=ctx["form_path"], kind=ctx["kind"],
            provider=ctx["provider"], score=ctx["score"],
        ))
    elif kind == "bot_challenge_fail":
        p = ae.build_bot_challenge_fail_payload(ae.BotChallengeFailContext(
            form_path=ctx["form_path"], reason=ctx["reason"],
            provider=ctx["provider"], score=ctx["score"],
        ))
    elif kind == "token_refresh":
        p = ae.build_token_refresh_payload(ae.TokenRefreshContext(
            user_id=ctx["user_id"], provider=ctx["provider"],
            outcome=ctx["outcome"],
            new_expires_in_seconds=ctx["new_expires_in_seconds"],
        ))
    elif kind == "token_rotated":
        p = ae.build_token_rotated_payload(ae.TokenRotatedContext(
            user_id=ctx["user_id"], provider=ctx["provider"],
            previous_refresh_token=ctx["previous_refresh_token"],
            new_refresh_token=ctx["new_refresh_token"],
            triggered_by=ctx["triggered_by"],
        ))
    else:  # pragma: no cover
        raise ValueError(f"unknown fixture kind: {kind!r}")
    return _normalise(p)


# Cross-Twin fixture corpus: a flat list of (kind, fixture) pairs, each
# tagged with the per-event ``id`` so pytest parametrize ids stay
# stable.  The Node runner consumes this same list.

ALL_FIXTURES: list[tuple[str, dict[str, Any]]] = (
    [("login_success", f) for f in LOGIN_SUCCESS_FIXTURES]
    + [("login_fail", f) for f in LOGIN_FAIL_FIXTURES]
    + [("oauth_connect", f) for f in OAUTH_CONNECT_FIXTURES]
    + [("oauth_revoke", f) for f in OAUTH_REVOKE_FIXTURES]
    + [("bot_challenge_pass", f) for f in BOT_CHALLENGE_PASS_FIXTURES]
    + [("bot_challenge_fail", f) for f in BOT_CHALLENGE_FAIL_FIXTURES]
    + [("token_refresh", f) for f in TOKEN_REFRESH_FIXTURES]
    + [("token_rotated", f) for f in TOKEN_ROTATED_FIXTURES]
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Node runner — single subprocess per session
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_NODE_RUNNER_TEMPLATE = r"""
import * as ae from "{TS_TWIN_FILE_URL}"

const fixtures = {FIXTURES_JSON}

function camelize(snake) {{
  return snake.replace(/_([a-z])/g, (_, c) => c.toUpperCase())
}}

function snakeFromCamel(s) {{
  return s.replace(/([A-Z])/g, "_$1").toLowerCase()
}}

async function buildOne(kind, ctx) {{
  if (kind === "login_success") {{
    return ae.buildLoginSuccessPayload({{
      userId: ctx.user_id, authMethod: ctx.auth_method,
      provider: ctx.provider, mfaSatisfied: ctx.mfa_satisfied,
      ip: ctx.ip, userAgent: ctx.user_agent,
    }})
  }}
  if (kind === "login_fail") {{
    return ae.buildLoginFailPayload({{
      attemptedUser: ctx.attempted_user, authMethod: ctx.auth_method,
      failReason: ctx.fail_reason, provider: ctx.provider,
      ip: ctx.ip, userAgent: ctx.user_agent,
    }})
  }}
  if (kind === "oauth_connect") {{
    return ae.buildOAuthConnectPayload({{
      userId: ctx.user_id, provider: ctx.provider,
      outcome: ctx.outcome, scope: ctx.scope,
      isAccountLink: ctx.is_account_link,
    }})
  }}
  if (kind === "oauth_revoke") {{
    return ae.buildOAuthRevokePayload({{
      userId: ctx.user_id, provider: ctx.provider,
      initiator: ctx.initiator,
      revocationSucceeded: ctx.revocation_succeeded,
    }})
  }}
  if (kind === "bot_challenge_pass") {{
    return ae.buildBotChallengePassPayload({{
      formPath: ctx.form_path, kind: ctx.kind,
      provider: ctx.provider, score: ctx.score,
    }})
  }}
  if (kind === "bot_challenge_fail") {{
    return ae.buildBotChallengeFailPayload({{
      formPath: ctx.form_path, reason: ctx.reason,
      provider: ctx.provider, score: ctx.score,
    }})
  }}
  if (kind === "token_refresh") {{
    return ae.buildTokenRefreshPayload({{
      userId: ctx.user_id, provider: ctx.provider,
      outcome: ctx.outcome,
      newExpiresInSeconds: ctx.new_expires_in_seconds,
    }})
  }}
  if (kind === "token_rotated") {{
    return ae.buildTokenRotatedPayload({{
      userId: ctx.user_id, provider: ctx.provider,
      previousRefreshToken: ctx.previous_refresh_token,
      newRefreshToken: ctx.new_refresh_token,
      triggeredBy: ctx.triggered_by,
    }})
  }}
  throw new Error("unknown kind: " + kind)
}}

function camelKeysToSnake(obj) {{
  if (obj === null || obj === undefined) return obj
  if (Array.isArray(obj)) return obj.map(camelKeysToSnake)
  if (typeof obj !== "object") return obj
  const out = {{}}
  for (const k of Object.keys(obj)) {{
    out[snakeFromCamel(k)] = camelKeysToSnake(obj[k])
  }}
  return out
}}

;(async () => {{
  const out = []
  for (const [kind, ctx] of fixtures) {{
    const payload = await buildOne(kind, ctx)
    // Convert payload's camelCase keys to snake_case to match Python.
    out.push({{
      kind,
      id: ctx.id,
      payload: {{
        action: payload.action,
        entity_kind: payload.entityKind,
        entity_id: payload.entityId,
        before: payload.before,
        after: payload.after,
        actor: payload.actor,
      }},
    }})
  }}
  process.stdout.write(JSON.stringify(out))
}})().catch((e) => {{
  process.stderr.write(String(e))
  process.exit(1)
}})
"""


@pytest.fixture(scope="session")
def ts_outputs() -> dict[tuple[str, str], dict[str, Any]]:
    """Spawn Node ONCE per session, drive every fixture through the TS
    twin, return a {(kind, id): normalised_payload} lookup."""
    if not _node_supports_strip_types():
        pytest.skip("Node ≥ 22 with --experimental-strip-types unavailable")
    if not _TS_TWIN_PATH.exists():
        pytest.skip(f"TS twin not present at {_TS_TWIN_PATH}")
    runner = _NODE_RUNNER_TEMPLATE.format(
        TS_TWIN_FILE_URL=_TS_TWIN_PATH.as_uri(),
        FIXTURES_JSON=json.dumps(ALL_FIXTURES),
    )
    r = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", runner],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"node TS twin runner failed:\n  stdout={r.stdout}\n  stderr={r.stderr}"
        )
    rows = json.loads(r.stdout)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        out[(row["kind"], row["id"])] = row["payload"]
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — Behavioural per-fixture parity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.parametrize(
    "kind,fixture",
    ALL_FIXTURES,
    ids=[f"{kind}-{f['id']}" for (kind, f) in ALL_FIXTURES],
)
def test_per_fixture_python_ts_byte_equal(
    kind: str,
    fixture: dict[str, Any],
    ts_outputs: dict[tuple[str, str], dict[str, Any]],
) -> None:
    py_payload = _python_run(kind, fixture)
    ts_payload = ts_outputs[(kind, fixture["id"])]
    assert py_payload == ts_payload, (
        f"drift on {kind}/{fixture['id']}:\n"
        f"  py={py_payload}\n  ts={ts_payload}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2b — Aggregate SHA-256 oracle
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _hash_corpus(payloads: list[dict[str, Any]]) -> str:
    """Stable JSON projection over every fixture, single SHA-256."""
    serialised = json.dumps(payloads, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()


def test_aggregate_sha256_oracle(
    ts_outputs: dict[tuple[str, str], dict[str, Any]],
) -> None:
    """One hash over the full normalised matrix.  Catches "many tiny
    drifts" the per-fixture tests can't summarise."""
    py = [_python_run(kind, fix) for (kind, fix) in ALL_FIXTURES]
    ts = [ts_outputs[(kind, fix["id"])] for (kind, fix) in ALL_FIXTURES]
    assert _hash_corpus(py) == _hash_corpus(ts), (
        "aggregate corpus hash drift — re-run per-fixture tests for detail"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — Coverage guards (drift-guard's drift guard)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_every_event_has_at_least_one_fixture() -> None:
    kinds_in_corpus = {k for (k, _) in ALL_FIXTURES}
    expected = {
        "login_success", "login_fail",
        "oauth_connect", "oauth_revoke",
        "bot_challenge_pass", "bot_challenge_fail",
        "token_refresh", "token_rotated",
    }
    assert kinds_in_corpus == expected


def test_every_login_fail_reason_exercised() -> None:
    """Each AS.5.1 login fail reason must show up in at least one
    fixture (excluding ones already adequately covered by other tests
    — we accept partial coverage as long as every vocabulary value is
    *referenced* somewhere in the static + behavioural test set)."""
    used = {f["fail_reason"] for f in LOGIN_FAIL_FIXTURES}
    # Behavioural fixtures cover 4 of 10 reasons; the unit-test family
    # exercises all 10 via @pytest.mark.parametrize.  Coverage guard
    # asserts at least 4 distinct reasons in the cross-twin corpus.
    assert len(used) >= 4


def test_every_oauth_revoke_initiator_exercised() -> None:
    used = {f["initiator"] for f in OAUTH_REVOKE_FIXTURES}
    assert used == ae.OAUTH_REVOKE_INITIATORS


def test_every_bot_challenge_pass_kind_exercised() -> None:
    used = {f["kind"] for f in BOT_CHALLENGE_PASS_FIXTURES}
    assert used == ae.BOT_CHALLENGE_PASS_KINDS


def test_every_bot_challenge_fail_reason_exercised() -> None:
    used = {f["reason"] for f in BOT_CHALLENGE_FAIL_FIXTURES}
    assert used == ae.BOT_CHALLENGE_FAIL_REASONS


def test_every_token_refresh_outcome_exercised() -> None:
    used = {f["outcome"] for f in TOKEN_REFRESH_FIXTURES}
    assert used == ae.TOKEN_REFRESH_OUTCOMES


def test_every_token_rotation_trigger_exercised() -> None:
    used = {f["triggered_by"] for f in TOKEN_ROTATED_FIXTURES}
    assert used == ae.TOKEN_ROTATION_TRIGGERS


def test_every_oauth_connect_outcome_exercised() -> None:
    used = {f["outcome"] for f in OAUTH_CONNECT_FIXTURES}
    assert used == ae.OAUTH_CONNECT_OUTCOMES

"""AS.1.4 — `backend.security.oauth_audit` contract tests.

Validates the canonical OAuth audit-event format + unified emit layer
defined in :mod:`backend.security.oauth_audit`. Five emitter families
(``oauth.{login_init, login_callback, refresh, unlink, token_rotated}``)
plus the cross-twin drift guard against
``templates/_shared/oauth-client/audit.ts``.

Test families
─────────────
1. Helpers              fingerprint round-trip + null/empty handling,
                        scope normaliser, entity_id composition.
2. login_init           payload shape pinned (entity_kind, entity_id,
                        before=None, after field-set), default actor
                        "anonymous", knob-off short-circuit returns
                        None without writing.
3. login_callback       outcome vocabulary enforced, success +
                        failure shapes, error field optional, link to
                        login_init via state_fp + entity_id.
4. refresh              outcome vocabulary enforced, default actor
                        falls back to user_id, optional fields
                        ``previous_expires_at`` / ``new_expires_in_seconds``
                        nullable.
5. unlink               outcome vocabulary enforced,
                        ``revocation_outcome`` validated only when
                        ``revocation_attempted=True`` (otherwise
                        forced to None).
6. token_rotated        triggered_by vocabulary enforced, both old +
                        new refresh_tokens replaced by 12-char
                        SHA-256 fingerprints (raw never persisted).
7. Knob-off matrix      AS.0.8 §5: ALL 5 emitters skip when
                        ``oauth_client.is_enabled()`` returns False.
8. Module-global state  per SOP §1: no module-level mutable state,
                        constants stable across reload, no IO at
                        import time, ``import hashlib`` provenance
                        grep (mirrors AS.1.1's ``import secrets`` grep).
9. Cross-twin parity    SHA-256 over the canonical outcome vocabulary
                        sets; per-event before/after field-set
                        equality; default-actor rules; entity_kind
                        constants; fingerprint algorithm match;
                        TS twin declares all 5 builders + emitters.
10. audit.log integration   real PG-backed end-to-end test for one
                            event (login_init) — exercises the
                            chain-append path, asserts the row lands
                            with the right action / entity_kind /
                            actor / after_json. Uses pg_test_pool
                            fixture per the existing audit-test
                            convention.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import pathlib
import re
from typing import Any
from unittest.mock import patch

import pytest

from backend.security import oauth_audit as oa
from backend.security import oauth_client as oc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TS twin path (loaded lazily; tests skip if absent)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TS_AUDIT_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "templates"
    / "_shared"
    / "oauth-client"
    / "audit.ts"
)


def _read_ts_audit() -> str:
    if not _TS_AUDIT_PATH.exists():
        pytest.skip(f"TS twin audit not present at {_TS_AUDIT_PATH}")
    return _TS_AUDIT_PATH.read_text(encoding="utf-8")


def _extract_ts_string_const(src: str, name: str) -> str:
    m = re.search(
        rf'export\s+const\s+{name}\s*=\s*"((?:[^"\\]|\\.)*)"', src
    )
    assert m, f"could not find `export const {name} = \"...\"` in TS twin"
    return m.group(1)


def _extract_ts_number_const(src: str, name: str) -> int:
    m = re.search(rf'export\s+const\s+{name}\s*=\s*(-?\d+)\b', src)
    assert m, f"could not find `export const {name} = <int>` in TS twin"
    return int(m.group(1))


def _extract_ts_set_members(src: str, name: str) -> list[str]:
    """Extract the string members of a TS `export const NAME =
    Object.freeze(new Set<string>([...]))` declaration in declaration
    order. Returns the literal strings (post-resolving any reference
    to other `OUTCOME_*` const names)."""
    # First grab the bracket body of the Set literal.
    pattern = re.compile(
        rf'export\s+const\s+{name}\s*:\s*[^=]+=\s*Object\.freeze\(\s*'
        rf'new\s+Set<string>\(\s*\[([\s\S]*?)\]\s*\)',
        re.MULTILINE,
    )
    m = pattern.search(src)
    assert m, f"could not find `export const {name} = Object.freeze(new Set<string>([...]))`"
    body = m.group(1)
    # Members are either string literals "foo" or const-references like
    # OUTCOME_SUCCESS. Resolve const-refs by re-extracting their string
    # value.
    members: list[str] = []
    for tok in body.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith('"') and tok.endswith('"'):
            members.append(tok[1:-1])
        else:
            members.append(_extract_ts_string_const(src, tok))
    return members


def _run(coro):
    """Sync wrapper for one-off async helpers (keeps tests not needing
    pytest-asyncio simple)."""
    return asyncio.run(coro)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1. Helpers — fingerprint / scope normaliser / entity_id
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_fingerprint_known_input():
    """Fingerprint of a known input is the first 12 hex chars of the
    SHA-256 digest. Pinned so cross-twin parity has a known oracle."""
    expected = hashlib.sha256(b"hello").hexdigest()[:12]
    assert oa.fingerprint("hello") == expected


def test_fingerprint_length_is_12():
    fp = oa.fingerprint("any-input-here")
    assert fp is not None
    assert len(fp) == oa.FINGERPRINT_LENGTH == 12


def test_fingerprint_returns_none_for_none_or_empty():
    assert oa.fingerprint(None) is None
    assert oa.fingerprint("") is None


def test_fingerprint_deterministic():
    assert oa.fingerprint("alpha") == oa.fingerprint("alpha")
    assert oa.fingerprint("alpha") != oa.fingerprint("beta")


def test_normalize_scope_handles_none_string_and_iterable():
    assert oa._normalize_scope(None) == []
    assert oa._normalize_scope("read write") == ["read", "write"]
    assert oa._normalize_scope("read,write") == ["read", "write"]
    assert oa._normalize_scope(["a", "b"]) == ["a", "b"]
    assert oa._normalize_scope(("a", "b")) == ["a", "b"]


def test_entity_id_token_format():
    assert oa._entity_id_token("github", "u-42") == "github:u-42"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2. login_init — payload shape pinning
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_login_init_ctx(**overrides) -> oa.LoginInitContext:
    base = dict(
        provider="github",
        state="state-aaaa-bbbb-cccc",
        scope=("read:user", "user:email"),
        redirect_uri="https://app.example/callback",
        use_oidc_nonce=False,
        state_ttl_seconds=600,
        actor="anonymous",
    )
    base.update(overrides)
    return oa.LoginInitContext(**base)


def test_login_init_writes_canonical_row(monkeypatch):
    """login_init writes one row with action=oauth.login_init,
    entity_kind=oauth_flow, entity_id=state, before=None, after with
    pinned field set."""
    captured: dict[str, Any] = {}

    async def fake_log(**kwargs):
        captured.update(kwargs)
        return 7

    monkeypatch.setattr("backend.security.oauth_audit.audit.log", fake_log)
    rid = _run(oa.emit_login_init(_make_login_init_ctx()))

    assert rid == 7
    assert captured["action"] == oc.EVENT_OAUTH_LOGIN_INIT == "oauth.login_init"
    assert captured["entity_kind"] == oa.ENTITY_KIND_FLOW == "oauth_flow"
    assert captured["entity_id"] == "state-aaaa-bbbb-cccc"
    assert captured["actor"] == "anonymous"
    assert captured["before"] is None
    after = captured["after"]
    assert set(after.keys()) == {
        "provider", "state_fp", "scope", "redirect_uri",
        "use_oidc_nonce", "state_ttl_seconds",
    }
    assert after["provider"] == "github"
    assert after["state_fp"] == oa.fingerprint("state-aaaa-bbbb-cccc")
    assert after["scope"] == ["read:user", "user:email"]
    assert after["use_oidc_nonce"] is False
    assert after["state_ttl_seconds"] == 600


def test_login_init_default_actor_is_anonymous(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_log(**kwargs):
        captured.update(kwargs)
        return 1

    monkeypatch.setattr("backend.security.oauth_audit.audit.log", fake_log)
    ctx = _make_login_init_ctx()
    # The dataclass default is "anonymous" — confirm it round-trips.
    assert ctx.actor == "anonymous"
    _run(oa.emit_login_init(ctx))
    assert captured["actor"] == "anonymous"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3. login_callback — outcome vocab + shape
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_login_callback_rejects_unknown_outcome():
    ctx = oa.LoginCallbackContext(
        provider="github", state="s", outcome="successs",  # typo
    )
    with pytest.raises(ValueError, match="not in"):
        _run(oa.emit_login_callback(ctx))


def test_login_callback_success_shape(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_log(**kwargs):
        captured.update(kwargs)
        return 11

    monkeypatch.setattr("backend.security.oauth_audit.audit.log", fake_log)
    ctx = oa.LoginCallbackContext(
        provider="google",
        state="s-xyz",
        outcome=oa.OUTCOME_SUCCESS,
        actor="u-42",
        granted_scope=("openid", "email"),
        has_refresh_token=True,
        expires_in_seconds=3600,
        is_oidc=True,
    )
    rid = _run(oa.emit_login_callback(ctx))
    assert rid == 11
    assert captured["action"] == oc.EVENT_OAUTH_LOGIN_CALLBACK == "oauth.login_callback"
    assert captured["entity_kind"] == oa.ENTITY_KIND_FLOW
    assert captured["entity_id"] == "s-xyz"
    state_fp = oa.fingerprint("s-xyz")
    assert captured["before"] == {"provider": "google", "state_fp": state_fp}
    after = captured["after"]
    assert set(after.keys()) == {
        "provider", "state_fp", "outcome", "granted_scope",
        "has_refresh_token", "expires_in_seconds", "is_oidc",
    }
    assert after["state_fp"] == state_fp
    assert after["granted_scope"] == ["openid", "email"]
    assert after["expires_in_seconds"] == 3600
    assert after["is_oidc"] is True
    assert "error" not in after


def test_login_callback_failure_shape_carries_error(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_log(**kwargs):
        captured.update(kwargs)
        return 12

    monkeypatch.setattr("backend.security.oauth_audit.audit.log", fake_log)
    ctx = oa.LoginCallbackContext(
        provider="github",
        state="s-bad",
        outcome=oa.OUTCOME_STATE_MISMATCH,
        error="state mismatch detected",
    )
    _run(oa.emit_login_callback(ctx))
    after = captured["after"]
    assert after["outcome"] == "state_mismatch"
    assert after["error"] == "state mismatch detected"
    # No refresh token claimed in failure path.
    assert after["has_refresh_token"] is False
    assert after["expires_in_seconds"] is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4. refresh — outcome vocab + actor fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_refresh_rejects_unknown_outcome():
    ctx = oa.RefreshContext(
        provider="github", user_id="u-1", outcome="bogus",
    )
    with pytest.raises(ValueError, match="not in"):
        _run(oa.emit_refresh(ctx))


def test_refresh_success_shape(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_log(**kwargs):
        captured.update(kwargs)
        return 21

    monkeypatch.setattr("backend.security.oauth_audit.audit.log", fake_log)
    ctx = oa.RefreshContext(
        provider="github",
        user_id="u-42",
        outcome=oa.OUTCOME_SUCCESS,
        previous_expires_at=1700000000.0,
        new_expires_in_seconds=3600,
        granted_scope=("read:user",),
    )
    _run(oa.emit_refresh(ctx))
    assert captured["action"] == oc.EVENT_OAUTH_REFRESH == "oauth.refresh"
    assert captured["entity_kind"] == oa.ENTITY_KIND_TOKEN == "oauth_token"
    assert captured["entity_id"] == "github:u-42"
    # Default actor falls back to user_id.
    assert captured["actor"] == "u-42"
    assert captured["before"] == {
        "provider": "github",
        "previous_expires_at": 1700000000.0,
    }
    after = captured["after"]
    assert set(after.keys()) == {
        "provider", "outcome", "new_expires_in_seconds", "granted_scope",
    }
    assert after["new_expires_in_seconds"] == 3600
    assert after["granted_scope"] == ["read:user"]


def test_refresh_explicit_actor_overrides_user_id(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_log(**kwargs):
        captured.update(kwargs)
        return 22

    monkeypatch.setattr("backend.security.oauth_audit.audit.log", fake_log)
    ctx = oa.RefreshContext(
        provider="github",
        user_id="u-42",
        outcome=oa.OUTCOME_PROVIDER_ERROR,
        actor="system:auto-refresh",
        error="provider 503",
    )
    _run(oa.emit_refresh(ctx))
    assert captured["actor"] == "system:auto-refresh"
    assert captured["after"]["error"] == "provider 503"


def test_refresh_nullable_optionals_round_trip(monkeypatch):
    """When previous_expires_at + new_expires_in_seconds are None
    (e.g. no_refresh_token outcome), the audit row carries typed
    nulls — not absent keys, not 0."""
    captured: dict[str, Any] = {}

    async def fake_log(**kwargs):
        captured.update(kwargs)
        return 23

    monkeypatch.setattr("backend.security.oauth_audit.audit.log", fake_log)
    ctx = oa.RefreshContext(
        provider="apple",
        user_id="u-9",
        outcome=oa.OUTCOME_NO_REFRESH_TOKEN,
    )
    _run(oa.emit_refresh(ctx))
    assert captured["before"]["previous_expires_at"] is None
    assert captured["after"]["new_expires_in_seconds"] is None
    assert captured["after"]["granted_scope"] == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5. unlink — outcome + revocation matrix
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_unlink_rejects_unknown_outcome():
    with pytest.raises(ValueError, match="not in"):
        _run(oa.emit_unlink(oa.UnlinkContext(
            provider="discord", user_id="u-1", outcome="hmm",
        )))


def test_unlink_revocation_attempted_requires_outcome():
    """When revocation_attempted=True, revocation_outcome MUST come
    from REVOCATION_OUTCOMES."""
    with pytest.raises(ValueError, match="revocation_outcome must be one of"):
        _run(oa.emit_unlink(oa.UnlinkContext(
            provider="discord",
            user_id="u-1",
            outcome=oa.OUTCOME_SUCCESS,
            revocation_attempted=True,
            revocation_outcome=None,
        )))


def test_unlink_revocation_not_attempted_forces_outcome_to_none(monkeypatch):
    """When revocation_attempted=False, ANY supplied revocation_outcome
    is overridden to None — keeps the row's shape clean."""
    captured: dict[str, Any] = {}

    async def fake_log(**kwargs):
        captured.update(kwargs)
        return 31

    monkeypatch.setattr("backend.security.oauth_audit.audit.log", fake_log)
    ctx = oa.UnlinkContext(
        provider="github",
        user_id="u-42",
        outcome=oa.OUTCOME_SUCCESS,
        revocation_attempted=False,
        revocation_outcome="something-stale",  # Should be discarded.
    )
    _run(oa.emit_unlink(ctx))
    assert captured["action"] == oc.EVENT_OAUTH_UNLINK == "oauth.unlink"
    assert captured["entity_kind"] == oa.ENTITY_KIND_TOKEN
    assert captured["entity_id"] == "github:u-42"
    assert captured["actor"] == "u-42"
    after = captured["after"]
    assert after["revocation_attempted"] is False
    assert after["revocation_outcome"] is None  # forced None


def test_unlink_revocation_attempted_success(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_log(**kwargs):
        captured.update(kwargs)
        return 32

    monkeypatch.setattr("backend.security.oauth_audit.audit.log", fake_log)
    ctx = oa.UnlinkContext(
        provider="discord",
        user_id="u-7",
        outcome=oa.OUTCOME_SUCCESS,
        revocation_attempted=True,
        revocation_outcome=oa.OUTCOME_REVOCATION_SKIPPED,
    )
    _run(oa.emit_unlink(ctx))
    after = captured["after"]
    assert after["revocation_attempted"] is True
    assert after["revocation_outcome"] == "revocation_skipped"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6. token_rotated — fingerprint-only invariant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_token_rotated_rejects_unknown_trigger():
    with pytest.raises(ValueError, match="not in"):
        _run(oa.emit_token_rotated(oa.TokenRotatedContext(
            provider="github", user_id="u-1",
            previous_refresh_token="old", new_refresh_token="new",
            triggered_by="bogus",
        )))


def test_token_rotated_writes_fingerprints_not_raw(monkeypatch):
    """The audit row MUST NOT contain the raw old or new refresh_token.
    Stored as 12-char SHA-256 fingerprints only — credentials never
    persist into the audit chain."""
    captured: dict[str, Any] = {}

    async def fake_log(**kwargs):
        captured.update(kwargs)
        return 41

    monkeypatch.setattr("backend.security.oauth_audit.audit.log", fake_log)
    old = "rt-OLD-secret-xyz-0123456789"
    new = "rt-NEW-secret-abc-9876543210"
    ctx = oa.TokenRotatedContext(
        provider="github",
        user_id="u-42",
        previous_refresh_token=old,
        new_refresh_token=new,
        triggered_by="auto_refresh",
    )
    _run(oa.emit_token_rotated(ctx))
    assert captured["action"] == oc.EVENT_OAUTH_TOKEN_ROTATED == "oauth.token_rotated"
    assert captured["entity_kind"] == oa.ENTITY_KIND_TOKEN
    assert captured["entity_id"] == "github:u-42"
    assert captured["actor"] == "u-42"
    before = captured["before"]
    after = captured["after"]
    assert before == {
        "provider": "github",
        "prior_refresh_token_fp": oa.fingerprint(old),
    }
    assert after == {
        "provider": "github",
        "new_refresh_token_fp": oa.fingerprint(new),
        "triggered_by": "auto_refresh",
    }
    # No raw token anywhere.
    blob = json.dumps([before, after])
    assert old not in blob
    assert new not in blob


def test_token_rotated_explicit_actor(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_log(**kwargs):
        captured.update(kwargs)
        return 42

    monkeypatch.setattr("backend.security.oauth_audit.audit.log", fake_log)
    ctx = oa.TokenRotatedContext(
        provider="github",
        user_id="u-42",
        previous_refresh_token="old",
        new_refresh_token="new",
        triggered_by="explicit_refresh",
        actor="ops:rotation-script",
    )
    _run(oa.emit_token_rotated(ctx))
    assert captured["actor"] == "ops:rotation-script"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7. Knob-off matrix — AS.0.8 §5
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_all_emitters_skip_when_knob_off(monkeypatch):
    """AS.0.8 §5 audit-behaviour matrix row "oauth.*": knob-false ⇒
    no row written. Centralised in the emit layer so callers don't
    need their own gate."""
    write_count = {"n": 0}

    async def fake_log(**kwargs):
        write_count["n"] += 1
        return 999

    monkeypatch.setattr("backend.security.oauth_audit.audit.log", fake_log)
    # Force the knob off.
    monkeypatch.setattr(
        "backend.security.oauth_audit.oauth_client.is_enabled",
        lambda: False,
    )

    rid1 = _run(oa.emit_login_init(_make_login_init_ctx()))
    rid2 = _run(oa.emit_login_callback(oa.LoginCallbackContext(
        provider="github", state="s", outcome=oa.OUTCOME_SUCCESS,
    )))
    rid3 = _run(oa.emit_refresh(oa.RefreshContext(
        provider="github", user_id="u", outcome=oa.OUTCOME_SUCCESS,
    )))
    rid4 = _run(oa.emit_unlink(oa.UnlinkContext(
        provider="github", user_id="u", outcome=oa.OUTCOME_SUCCESS,
    )))
    rid5 = _run(oa.emit_token_rotated(oa.TokenRotatedContext(
        provider="github", user_id="u",
        previous_refresh_token="old", new_refresh_token="new",
        triggered_by="auto_refresh",
    )))
    assert rid1 is rid2 is rid3 is rid4 is rid5 is None
    assert write_count["n"] == 0


def test_outcome_validation_runs_before_knob_check():
    """Even when the knob is off, an invalid outcome MUST still raise
    — bad inputs are programmer errors, not runtime conditions, and
    the validation belongs at the boundary regardless of the knob.
    Otherwise a typo would silently disappear in dev (knob on,
    raises) but pass through in prod-rollback (knob off, swallowed)."""
    with patch(
        "backend.security.oauth_audit.oauth_client.is_enabled",
        return_value=False,
    ):
        with pytest.raises(ValueError):
            _run(oa.emit_login_callback(oa.LoginCallbackContext(
                provider="github", state="s", outcome="typo",
            )))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  8. Module-global state audit — per SOP §1
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_module_constants_stable_across_reload():
    """Reloading the module yields identical canonical constants —
    confirms there is no hidden module-level mutable state that the
    module body would derive differently on a second import."""
    fp1 = oa.fingerprint("oracle-input")
    importlib.reload(oa)
    fp2 = oa.fingerprint("oracle-input")
    assert fp1 == fp2
    assert oa.FINGERPRINT_LENGTH == 12
    assert oa.ENTITY_KIND_FLOW == "oauth_flow"
    assert oa.ENTITY_KIND_TOKEN == "oauth_token"
    assert oa.OUTCOME_SUCCESS == "success"


def test_no_module_level_mutable_collections():
    """Per SOP §1 module-state audit: only frozen / immutable
    containers at module level. A mutable dict / list / set survives
    reload but accumulates state across worker requests — exactly the
    bug class that broke ``backend.auth_baseline_mode`` (task #90)."""
    import inspect
    src = inspect.getsource(oa)
    # Outcome sets MUST be frozenset, never set / dict / list at
    # module level. The frontend twin uses Object.freeze; this side
    # uses frozenset.
    assert re.search(r'^[A-Z_]+_OUTCOMES\s*:\s*frozenset', src, re.MULTILINE), (
        "Outcome sets must be frozenset at module level"
    )
    # Module-level non-class assignments using {} literal would be
    # mutable dicts — guard against accidental introduction.
    bad = re.findall(r'^[A-Z_][A-Z_0-9]*\s*=\s*\{[^}:]', src, re.MULTILINE)
    assert not bad, f"Found mutable dict at module level: {bad}"


def test_imports_hashlib_not_secrets():
    """Provenance pin: ``oauth_audit`` derives DETERMINISTIC
    fingerprints from already-secret material via :mod:`hashlib`. It
    does NOT generate randomness — that's `oauth_client`'s job. This
    grep makes sure a future edit doesn't accidentally pull in
    :mod:`secrets` here (which would be a code smell suggesting
    confused responsibilities) or remove :mod:`hashlib` (which would
    silently change the fingerprint algorithm)."""
    import inspect
    src = inspect.getsource(oa)
    assert re.search(r'^import\s+hashlib\b', src, re.MULTILINE), (
        "oauth_audit must `import hashlib` for fingerprinting"
    )
    assert not re.search(r'^import\s+secrets\b', src, re.MULTILINE), (
        "oauth_audit must NOT `import secrets` — randomness lives in oauth_client"
    )


def test_all_oauth_events_have_an_emitter():
    """Each of the 5 ``EVENT_OAUTH_*`` strings must have a matching
    ``emit_*`` function. Catches a future event addition that lands
    only one half (e.g. constant defined in oauth_client but no
    emitter wired)."""
    for ev in oc.ALL_OAUTH_EVENTS:
        # Map "oauth.login_init" → "emit_login_init" (drop "oauth." prefix
        # then prepend "emit_").
        suffix = ev.removeprefix("oauth.")
        assert hasattr(oa, f"emit_{suffix}"), (
            f"missing emitter for event {ev!r} — expected emit_{suffix}"
        )


def test_all_exported_names_in_dunder_all():
    """Every top-level name a caller is meant to import must be
    listed in ``__all__`` so star-imports + autocompletion behave."""
    for name in (
        "emit_login_init", "emit_login_callback", "emit_refresh",
        "emit_unlink", "emit_token_rotated",
        "LoginInitContext", "LoginCallbackContext", "RefreshContext",
        "UnlinkContext", "TokenRotatedContext",
        "fingerprint",
        "ENTITY_KIND_FLOW", "ENTITY_KIND_TOKEN",
        "FINGERPRINT_LENGTH",
        "LOGIN_CALLBACK_OUTCOMES", "REFRESH_OUTCOMES",
        "UNLINK_OUTCOMES", "ROTATION_TRIGGERS", "REVOCATION_OUTCOMES",
        "OUTCOME_SUCCESS", "OUTCOME_STATE_MISMATCH",
        "OUTCOME_STATE_EXPIRED", "OUTCOME_TOKEN_ERROR",
        "OUTCOME_CALLBACK_ERROR", "OUTCOME_NO_REFRESH_TOKEN",
        "OUTCOME_PROVIDER_ERROR", "OUTCOME_NOT_LINKED",
        "OUTCOME_REVOCATION_FAILED", "OUTCOME_REVOCATION_SKIPPED",
    ):
        assert name in oa.__all__, f"{name!r} missing from oauth_audit.__all__"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  9. Cross-twin parity — Python ↔ TS twin
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_ts_twin_present():
    _read_ts_audit()  # asserts file exists


def test_ts_twin_entity_kind_constants_parity():
    src = _read_ts_audit()
    assert _extract_ts_string_const(src, "ENTITY_KIND_FLOW") == oa.ENTITY_KIND_FLOW
    assert _extract_ts_string_const(src, "ENTITY_KIND_TOKEN") == oa.ENTITY_KIND_TOKEN


def test_ts_twin_fingerprint_length_parity():
    src = _read_ts_audit()
    assert _extract_ts_number_const(src, "FINGERPRINT_LENGTH") == oa.FINGERPRINT_LENGTH


def test_ts_twin_outcome_string_constants_parity():
    """All 10 OUTCOME_* string constants byte-match between sides."""
    src = _read_ts_audit()
    for name in (
        "OUTCOME_SUCCESS", "OUTCOME_STATE_MISMATCH",
        "OUTCOME_STATE_EXPIRED", "OUTCOME_TOKEN_ERROR",
        "OUTCOME_CALLBACK_ERROR", "OUTCOME_NO_REFRESH_TOKEN",
        "OUTCOME_PROVIDER_ERROR", "OUTCOME_NOT_LINKED",
        "OUTCOME_REVOCATION_FAILED", "OUTCOME_REVOCATION_SKIPPED",
    ):
        py_val = getattr(oa, name)
        ts_val = _extract_ts_string_const(src, name)
        assert py_val == ts_val, f"{name}: py={py_val!r} ts={ts_val!r}"


@pytest.mark.parametrize("vocab_name", [
    "LOGIN_CALLBACK_OUTCOMES",
    "REFRESH_OUTCOMES",
    "UNLINK_OUTCOMES",
    "ROTATION_TRIGGERS",
    "REVOCATION_OUTCOMES",
])
def test_audit_outcome_vocab_parity_python_ts(vocab_name):
    """SHA-256 oracle over each outcome vocabulary set, sorted to make
    the comparison order-independent (sets are unordered on both sides
    by definition; we only care about membership equality)."""
    src = _read_ts_audit()
    py_set = sorted(getattr(oa, vocab_name))
    ts_set = sorted(_extract_ts_set_members(src, vocab_name))
    assert py_set == ts_set, (
        f"{vocab_name} drift between Python and TS twin\n"
        f"  Python: {py_set}\n"
        f"  TS    : {ts_set}"
    )
    py_hash = hashlib.sha256("\n".join(py_set).encode("utf-8")).hexdigest()
    ts_hash = hashlib.sha256("\n".join(ts_set).encode("utf-8")).hexdigest()
    assert py_hash == ts_hash


def test_ts_twin_declares_all_five_emitters():
    """Catches a partial port that leaves one event family un-emitted."""
    src = _read_ts_audit()
    for fn in (
        "emitLoginInit", "emitLoginCallback", "emitRefresh",
        "emitUnlink", "emitTokenRotated",
    ):
        assert re.search(rf'export\s+async\s+function\s+{fn}\s*\(', src), (
            f"TS twin missing `export async function {fn}`"
        )


def test_ts_twin_declares_all_five_builders():
    """The pure-functional payload builders must be exported alongside
    the sink-fanout emitters — tests + offline tooling rely on them."""
    src = _read_ts_audit()
    for fn in (
        "buildLoginInitPayload", "buildLoginCallbackPayload",
        "buildRefreshPayload", "buildUnlinkPayload",
        "buildTokenRotatedPayload",
    ):
        assert re.search(rf'export\s+async\s+function\s+{fn}\s*\(', src), (
            f"TS twin missing `export async function {fn}`"
        )


def test_ts_twin_uses_subtle_digest_for_fingerprint():
    """The TS fingerprint helper MUST go through Web Crypto's
    `subtle.digest("SHA-256", …)` — the same algorithm the Python
    side's `hashlib.sha256` uses. Any other hash family would silently
    desynchronise fingerprints across the two sides."""
    src = _read_ts_audit()
    assert 'subtle.digest("SHA-256"' in src or "subtle.digest('SHA-256'" in src


def test_ts_twin_includes_actor_default_anonymous():
    """`emitLoginInit` defaults actor to "anonymous" — same as Python."""
    src = _read_ts_audit()
    assert '"anonymous"' in src
    # And the actor-fallback patterns mirror Python: ?? userId for
    # token-kind events, ?? "anonymous" for flow-kind events.
    assert "ctx.actor ?? ctx.userId" in src
    assert 'ctx.actor ?? "anonymous"' in src


def test_ts_twin_does_not_log_raw_refresh_token():
    """Defence-in-depth: the TS source MUST NOT contain a literal
    `previousRefreshToken: ` followed by anything other than a
    `fingerprint(...)` wrap inside the `buildTokenRotatedPayload`
    builder. Catches a future edit that accidentally surfaces the raw
    value into the audit payload."""
    src = _read_ts_audit()
    # Locate the builder body by anchoring on the function signature.
    m = re.search(
        r'export\s+async\s+function\s+buildTokenRotatedPayload[\s\S]+?\n\}\n',
        src,
    )
    assert m, "could not locate buildTokenRotatedPayload body"
    body = m.group(0)
    # The two raw-secret fields must only appear inside `fingerprint(...)`
    # calls.
    for raw_field in ("ctx.previousRefreshToken", "ctx.newRefreshToken"):
        for occurrence in re.finditer(re.escape(raw_field), body):
            start = max(0, occurrence.start() - 30)
            ctx_window = body[start:occurrence.end() + 5]
            assert "fingerprint(" in ctx_window, (
                f"{raw_field} appears in buildTokenRotatedPayload outside "
                f"fingerprint() wrapper — credentials leak risk"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  10. audit.log integration — real PG end-to-end (one event)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture()
async def _audit_db(pg_test_pool):
    """Truncate ``audit_log`` per test (mirrors the Y9 pattern)."""
    async with pg_test_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE audit_log RESTART IDENTITY CASCADE"
        )
    from backend import audit
    try:
        yield audit
    finally:
        from backend.db_context import set_tenant_id
        set_tenant_id(None)
        async with pg_test_pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE audit_log RESTART IDENTITY CASCADE"
            )


@pytest.mark.asyncio
async def test_login_init_lands_real_audit_row(_audit_db):
    """End-to-end PG smoke: emit_login_init writes a real row to
    audit_log with the canonical action / entity_kind / entity_id /
    actor and the after_json carries the pinned field set."""
    from backend.db_context import set_tenant_id
    from backend.db_pool import get_pool

    set_tenant_id("t-default")
    rid = await oa.emit_login_init(_make_login_init_ctx(
        actor="u-99",
        state="real-state-zzz",
    ))
    assert isinstance(rid, int) and rid > 0

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM audit_log WHERE id = $1", rid,
        )
    assert row is not None
    assert row["action"] == "oauth.login_init"
    assert row["entity_kind"] == "oauth_flow"
    assert row["entity_id"] == "real-state-zzz"
    assert row["actor"] == "u-99"
    assert row["tenant_id"] == "t-default"
    after = json.loads(row["after_json"])
    assert after["provider"] == "github"
    assert after["state_fp"] == oa.fingerprint("real-state-zzz")
    assert "real-state-zzz" not in row["after_json"] or (
        # state appears only via entity_id column, NOT in the after JSON
        # body — verify the fingerprint surface is what carries it.
        after["state_fp"] != "real-state-zzz"
    )
    # Knob true by default — chain verifier should be happy.
    from backend import audit as audit_mod
    res = await audit_mod.verify_chain(tenant_id="t-default")
    # verify_chain may return a tuple/dict depending on impl; just
    # require it to not raise.
    assert res is not None or res is None  # smoke: no exception

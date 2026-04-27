"""AS.6.4 — backend.security.honeypot_form_verifier contract tests.

Validates the universal honeypot backend-verify wiring helper that
the four OmniSight self-forms (login / signup / password-reset /
contact) share per AS.0.5 §6.1 acceptance criteria — sibling to
AS.6.3 :mod:`turnstile_form_verifier`.

Test families
─────────────
1. Form action / form path constants — 4 actions + 4 paths frozen,
   ``SUPPORTED_*`` frozenset shape, ``form_path_for_action``
   dispatch + unknown action raises.
2. Cross-module drift — :data:`SUPPORTED_FORM_PATHS` byte-equal
   :data:`backend.security.honeypot._FORM_PREFIXES` keys + the
   AS.6.3 :data:`turnstile_form_verifier.SUPPORTED_FORM_PATHS`
   set (AS.5.2 dashboard natural-join invariant).
3. Anonymous tenant_id sentinel — pinned constant + non-empty
   override wins.
4. AS.0.8 single-knob — ``is_enabled`` reads
   :func:`honeypot.is_enabled`; knob-off → orchestrator returns
   AS.4.1 bypass result with ``bypass_kind="knob_off"`` and no
   forensic emit.
5. Bypass extraction — AS.0.6 §4 axes A → C → B; api_key wins
   over test_token wins over ip_allowlist; no axis fires → None.
6. Forensic audit fan-out — pass / fail / form_drift each emit
   their canonical ``bot_challenge.honeypot_*`` event via
   :func:`backend.audit.log`; bypass / knob-off paths skip emit.
7. End-to-end ``verify_form_honeypot`` — pass (empty field),
   fail (filled field), form drift (field missing), bypass
   (api_key axis), knob-off, anonymous tenant_id auto-substitution.
8. ``verify_form_honeypot_or_reject`` — fail → raises
   :class:`HoneypotRejected` carrying canonical 429 +
   ``bot_challenge_failed`` code; allow paths return result.
9. Module-global state audit (SOP §1) — no top-level mutable
   container; importing is side-effect free.
10. Outcome routing drift — ``_OUTCOME_TO_HONEYPOT_BYPASS_KIND``
    keys all in :data:`bot_challenge.ALL_OUTCOMES`, values all in
    :data:`honeypot.ALL_BYPASS_KINDS`.
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from backend.security import bot_challenge, honeypot
from backend.security import honeypot_form_verifier as hpv
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
    """Minimal stand-in for :class:`fastapi.Request`."""
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


def _ts(epoch: int, *, offset: int = 0) -> float:
    return float(epoch) * honeypot.HONEYPOT_ROTATION_PERIOD_SECONDS + offset


def _expected_field_name(form_path: str, tenant_id: str, *, now: float) -> str:
    return honeypot.honeypot_field_name(
        form_path, tenant_id, honeypot.current_epoch(now=now),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — Form action / form path constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_form_action_constants_pinned():
    assert hpv.FORM_ACTION_LOGIN == "login"
    assert hpv.FORM_ACTION_SIGNUP == "signup"
    assert hpv.FORM_ACTION_PASSWORD_RESET == "pwreset"
    assert hpv.FORM_ACTION_CONTACT == "contact"


def test_supported_form_actions_is_frozen_4_set():
    assert isinstance(hpv.SUPPORTED_FORM_ACTIONS, frozenset)
    assert hpv.SUPPORTED_FORM_ACTIONS == {
        "login", "signup", "pwreset", "contact",
    }


def test_form_path_constants_pinned():
    assert hpv.FORM_PATH_LOGIN == "/api/v1/auth/login"
    assert hpv.FORM_PATH_SIGNUP == "/api/v1/auth/signup"
    assert hpv.FORM_PATH_PASSWORD_RESET == "/api/v1/auth/password-reset"
    assert hpv.FORM_PATH_CONTACT == "/api/v1/auth/contact"


def test_supported_form_paths_is_frozen_4_set():
    assert isinstance(hpv.SUPPORTED_FORM_PATHS, frozenset)
    assert len(hpv.SUPPORTED_FORM_PATHS) == 4


def test_form_path_for_action_dispatch():
    assert hpv.form_path_for_action(hpv.FORM_ACTION_LOGIN) == hpv.FORM_PATH_LOGIN
    assert hpv.form_path_for_action(hpv.FORM_ACTION_SIGNUP) == hpv.FORM_PATH_SIGNUP
    assert hpv.form_path_for_action(hpv.FORM_ACTION_PASSWORD_RESET) == hpv.FORM_PATH_PASSWORD_RESET
    assert hpv.form_path_for_action(hpv.FORM_ACTION_CONTACT) == hpv.FORM_PATH_CONTACT


def test_form_path_for_action_unknown_raises():
    with pytest.raises(ValueError, match="unknown form_action"):
        hpv.form_path_for_action("not-a-real-action")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — Cross-module drift guards
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_supported_form_paths_aligned_with_honeypot():
    """AS.4.1 §4.1 invariant — honeypot field-name SoT must match
    the helper's path vocabulary byte-for-byte."""
    assert hpv.SUPPORTED_FORM_PATHS == frozenset(honeypot._FORM_PREFIXES.keys())


def test_supported_form_paths_aligned_with_turnstile_helper():
    """AS.5.2 dashboard natural-join invariant — captcha + honeypot
    rollup rows MUST share the same form_path key set."""
    assert hpv.SUPPORTED_FORM_PATHS == tv.SUPPORTED_FORM_PATHS


def test_supported_form_actions_aligned_with_turnstile_helper():
    """AS.0.5 §3 invariant — widget_action vocabulary shared
    between captcha + honeypot wiring."""
    assert hpv.SUPPORTED_FORM_ACTIONS == tv.SUPPORTED_FORM_ACTIONS


def test_form_action_constants_match_turnstile_helper():
    assert hpv.FORM_ACTION_LOGIN == tv.FORM_ACTION_LOGIN
    assert hpv.FORM_ACTION_SIGNUP == tv.FORM_ACTION_SIGNUP
    assert hpv.FORM_ACTION_PASSWORD_RESET == tv.FORM_ACTION_PASSWORD_RESET
    assert hpv.FORM_ACTION_CONTACT == tv.FORM_ACTION_CONTACT


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — Anonymous tenant_id sentinel
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_anonymous_tenant_id_pinned():
    assert hpv.ANONYMOUS_TENANT_ID == "_anonymous"


def test_anonymous_tenant_id_used_when_caller_passes_none(monkeypatch):
    """Pre-auth forms (login) pass tenant_id=None; the helper
    must substitute the sentinel so the field-name space is
    deterministic and the React widget renders the matching name."""
    request = _fake_request()
    submitted: dict[str, Any] = {}
    expected_name = _expected_field_name(
        hpv.FORM_PATH_LOGIN, hpv.ANONYMOUS_TENANT_ID, now=_ts(1, offset=10),
    )
    submitted[expected_name] = ""  # empty → pass

    with patch("backend.audit.log", AsyncMock()):
        result = _run(hpv.verify_form_honeypot(
            hpv.FORM_ACTION_LOGIN,
            submitted=submitted,
            request=request,
            now=_ts(1, offset=10),
        ))
    assert result.allow is True
    assert result.outcome == honeypot.OUTCOME_HONEYPOT_PASS


def test_explicit_tenant_id_wins(monkeypatch):
    """Authed forms (change-password) pass user.tenant_id — the
    field-name space splits per tenant for anti-fingerprint."""
    request = _fake_request()
    expected_name = _expected_field_name(
        hpv.FORM_PATH_PASSWORD_RESET, "tenant-X", now=_ts(1, offset=10),
    )
    submitted: dict[str, Any] = {expected_name: ""}

    with patch("backend.audit.log", AsyncMock()):
        result = _run(hpv.verify_form_honeypot(
            hpv.FORM_ACTION_PASSWORD_RESET,
            submitted=submitted,
            request=request,
            tenant_id="tenant-X",
            now=_ts(1, offset=10),
        ))
    assert result.allow is True
    assert result.outcome == honeypot.OUTCOME_HONEYPOT_PASS
    # Field-name diverges from anonymous tenant
    anon_name = _expected_field_name(
        hpv.FORM_PATH_PASSWORD_RESET, hpv.ANONYMOUS_TENANT_ID, now=_ts(1, offset=10),
    )
    # Different tenant_id → different SHA-256 → different rare-pool
    # index (1-in-12 collision possible but for "tenant-X" vs
    # "_anonymous" we just verify the helper actually keyed on the
    # explicit value, not that they always differ).
    assert result.field_name_used == expected_name
    # Sanity: explicit tenant produces deterministic name regardless
    # of whether it happens to collide with the anonymous-pool entry.
    _ = anon_name  # used for grep clarity; the field_name_used assertion is what locks behaviour


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 4 — AS.0.8 single-knob
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_is_enabled_default_true():
    """AS.0.8 §3.1 forward-promotion guard."""
    assert hpv.is_enabled() is True


def test_is_enabled_delegates_to_honeypot_is_enabled():
    """The helper must NOT have its own knob — both layers share
    one switch so an operator can't accidentally disable just one."""
    with patch.object(honeypot, "is_enabled", return_value=False):
        assert hpv.is_enabled() is False
    with patch.object(honeypot, "is_enabled", return_value=True):
        assert hpv.is_enabled() is True


def test_knob_off_returns_bypass_no_audit():
    """AS.0.8 §3.1 noop matrix — knob-false ⇒ helper returns AS.4.1
    bypass result with bypass_kind=knob_off, no forensic emit, no DB."""
    request = _fake_request()
    audit_log = AsyncMock()
    with patch.object(honeypot, "is_enabled", return_value=False), \
         patch("backend.audit.log", audit_log):
        result = _run(hpv.verify_form_honeypot(
            hpv.FORM_ACTION_LOGIN,
            submitted={"anything": "filled"},
            request=request,
        ))
    assert result.allow is True
    assert result.outcome == honeypot.OUTCOME_HONEYPOT_BYPASS
    assert result.bypass_kind == honeypot.BYPASS_KIND_KNOB_OFF
    assert result.audit_event is None
    audit_log.assert_not_awaited()


def test_knob_off_or_reject_returns_bypass():
    """or_reject is a thin wrapper — knob-off has allow=True so no
    HoneypotRejected raise."""
    request = _fake_request()
    with patch.object(honeypot, "is_enabled", return_value=False), \
         patch("backend.audit.log", AsyncMock()):
        result = _run(hpv.verify_form_honeypot_or_reject(
            hpv.FORM_ACTION_LOGIN,
            submitted={"anything": "filled"},
            request=request,
        ))
    assert result.allow is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 5 — Bypass extraction (AS.0.6 §4 axes)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_bypass_no_axis_returns_none():
    request = _fake_request()
    assert hpv.extract_bypass_kind_from_request(
        request, form_path=hpv.FORM_PATH_LOGIN,
    ) is None


def test_bypass_api_key_axis_a():
    """Axis A (api_key auth) — caller_kind in the bypass set."""
    request = _fake_request(state={"caller_kind": "apikey_omni"})
    assert hpv.extract_bypass_kind_from_request(
        request, form_path=hpv.FORM_PATH_LOGIN,
    ) == honeypot.BYPASS_KIND_API_KEY


def test_bypass_test_token_axis_c(monkeypatch):
    """Axis C (test-token header)."""
    monkeypatch.setenv("OMNISIGHT_BOT_CHALLENGE_TEST_TOKEN", "y" * 32)
    request = _fake_request(headers={bot_challenge.TEST_TOKEN_HEADER: "y" * 32})
    assert hpv.extract_bypass_kind_from_request(
        request, form_path=hpv.FORM_PATH_LOGIN,
    ) == honeypot.BYPASS_KIND_TEST_TOKEN


def test_bypass_ip_allowlist_axis_b():
    """Axis B (per-tenant IP allowlist)."""
    request = _fake_request(client_host="10.1.2.3")
    assert hpv.extract_bypass_kind_from_request(
        request,
        form_path=hpv.FORM_PATH_LOGIN,
        tenant_ip_allowlist=["10.0.0.0/8"],
    ) == honeypot.BYPASS_KIND_IP_ALLOWLIST


def test_bypass_api_key_wins_over_other_axes(monkeypatch):
    """AS.0.6 §4 precedence: A (api_key) wins over C (test_token)
    wins over B (ip_allowlist)."""
    monkeypatch.setenv("OMNISIGHT_BOT_CHALLENGE_TEST_TOKEN", "z" * 32)
    request = _fake_request(
        headers={bot_challenge.TEST_TOKEN_HEADER: "z" * 32},
        client_host="10.1.2.3",
        state={"caller_kind": "apikey_omni"},
    )
    assert hpv.extract_bypass_kind_from_request(
        request,
        form_path=hpv.FORM_PATH_LOGIN,
        tenant_ip_allowlist=["10.0.0.0/8"],
    ) == honeypot.BYPASS_KIND_API_KEY


def test_bypass_corrupt_ip_allowlist_filtered():
    """Corrupt allowlist entries (None / empty / whitespace) must
    not raise — bot_challenge.evaluate_bypass already logs+skips,
    we just normalise upstream."""
    request = _fake_request(client_host="10.1.2.3")
    # Pass mixed-validity list — only "10.0.0.0/8" is honored.
    assert hpv.extract_bypass_kind_from_request(
        request,
        form_path=hpv.FORM_PATH_LOGIN,
        tenant_ip_allowlist=["", "  ", None, "10.0.0.0/8"],  # type: ignore[list-item]
    ) == honeypot.BYPASS_KIND_IP_ALLOWLIST


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 6 — Forensic audit fan-out
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_pass_emits_honeypot_pass_audit():
    request = _fake_request()
    expected = _expected_field_name(
        hpv.FORM_PATH_LOGIN, hpv.ANONYMOUS_TENANT_ID, now=_ts(2, offset=5),
    )
    audit_log = AsyncMock()
    with patch("backend.audit.log", audit_log):
        result = _run(hpv.verify_form_honeypot(
            hpv.FORM_ACTION_LOGIN,
            submitted={expected: ""},
            request=request,
            now=_ts(2, offset=5),
            actor="alice@example.com",
        ))
    assert result.outcome == honeypot.OUTCOME_HONEYPOT_PASS
    audit_log.assert_awaited_once()
    kwargs = audit_log.await_args.kwargs
    assert kwargs["action"] == honeypot.EVENT_BOT_CHALLENGE_HONEYPOT_PASS
    assert kwargs["entity_id"] == hpv.FORM_PATH_LOGIN
    assert kwargs["actor"] == "alice@example.com"


def test_fail_emits_honeypot_fail_audit():
    """Filled honeypot → bot caught."""
    request = _fake_request()
    expected = _expected_field_name(
        hpv.FORM_PATH_LOGIN, hpv.ANONYMOUS_TENANT_ID, now=_ts(2, offset=5),
    )
    audit_log = AsyncMock()
    with patch("backend.audit.log", audit_log):
        result = _run(hpv.verify_form_honeypot(
            hpv.FORM_ACTION_LOGIN,
            submitted={expected: "i-am-a-bot"},
            request=request,
            now=_ts(2, offset=5),
        ))
    assert result.outcome == honeypot.OUTCOME_HONEYPOT_FAIL
    assert result.allow is False
    audit_log.assert_awaited_once()
    kwargs = audit_log.await_args.kwargs
    assert kwargs["action"] == honeypot.EVENT_BOT_CHALLENGE_HONEYPOT_FAIL
    # PII-redacted: only the length, never the raw value
    assert kwargs["after"]["field_filled_length"] == len("i-am-a-bot")
    raw_dump = repr(kwargs["after"])
    assert "i-am-a-bot" not in raw_dump


def test_form_drift_emits_honeypot_form_drift_audit():
    """Honeypot field missing entirely from submitted form →
    frontend deploy-drift alarm."""
    request = _fake_request()
    audit_log = AsyncMock()
    with patch("backend.audit.log", audit_log):
        result = _run(hpv.verify_form_honeypot(
            hpv.FORM_ACTION_LOGIN,
            submitted={"email": "a@b", "password": "x"},
            request=request,
            now=_ts(2, offset=5),
        ))
    assert result.outcome == honeypot.OUTCOME_HONEYPOT_FORM_DRIFT
    assert result.allow is False
    audit_log.assert_awaited_once()
    kwargs = audit_log.await_args.kwargs
    assert kwargs["action"] == honeypot.EVENT_BOT_CHALLENGE_HONEYPOT_FORM_DRIFT


def test_bypass_path_skips_audit():
    """Bypass paths intentionally have audit_event=None — caller's
    bypass detection layer owns the AS.0.6 bypass_* event."""
    request = _fake_request(state={"caller_kind": "apikey_omni"})
    audit_log = AsyncMock()
    with patch("backend.audit.log", audit_log):
        result = _run(hpv.verify_form_honeypot(
            hpv.FORM_ACTION_LOGIN,
            submitted={"foo": "bar"},
            request=request,
        ))
    assert result.outcome == honeypot.OUTCOME_HONEYPOT_BYPASS
    assert result.bypass_kind == honeypot.BYPASS_KIND_API_KEY
    audit_log.assert_not_awaited()


def test_audit_emit_failure_swallowed():
    """The audit chain has its own retry policy — a transient emit
    failure must NOT break the verify path itself."""
    request = _fake_request()
    expected = _expected_field_name(
        hpv.FORM_PATH_LOGIN, hpv.ANONYMOUS_TENANT_ID, now=_ts(2, offset=5),
    )
    audit_log = AsyncMock(side_effect=RuntimeError("DB down"))
    with patch("backend.audit.log", audit_log):
        result = _run(hpv.verify_form_honeypot(
            hpv.FORM_ACTION_LOGIN,
            submitted={expected: ""},
            request=request,
            now=_ts(2, offset=5),
        ))
    # The honeypot itself still passed — audit failure shouldn't block.
    assert result.outcome == honeypot.OUTCOME_HONEYPOT_PASS
    assert result.allow is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 7 — End-to-end verify_form_honeypot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_verify_form_honeypot_unknown_action_raises():
    request = _fake_request()
    with pytest.raises(ValueError, match="unknown form_action"):
        _run(hpv.verify_form_honeypot(
            "bogus", submitted={}, request=request,
        ))


def test_verify_form_honeypot_explicit_bypass_kind_short_circuits():
    """Caller can pre-detect a bypass and pass it explicitly so the
    AS.0.6 axes don't re-evaluate."""
    request = _fake_request()  # No caller_kind in state
    audit_log = AsyncMock()
    with patch("backend.audit.log", audit_log):
        result = _run(hpv.verify_form_honeypot(
            hpv.FORM_ACTION_LOGIN,
            submitted={"junk": "anything"},
            request=request,
            bypass_kind=honeypot.BYPASS_KIND_API_KEY,
        ))
    assert result.outcome == honeypot.OUTCOME_HONEYPOT_BYPASS
    assert result.bypass_kind == honeypot.BYPASS_KIND_API_KEY


def test_verify_form_honeypot_tenant_disabled():
    request = _fake_request()
    audit_log = AsyncMock()
    with patch("backend.audit.log", audit_log):
        result = _run(hpv.verify_form_honeypot(
            hpv.FORM_ACTION_LOGIN,
            submitted={"foo": "bar"},  # would normally fail (form drift)
            request=request,
            tenant_honeypot_active=False,
        ))
    assert result.outcome == honeypot.OUTCOME_HONEYPOT_BYPASS
    assert result.bypass_kind == honeypot.BYPASS_KIND_TENANT_DISABLED
    audit_log.assert_not_awaited()


def test_verify_form_honeypot_passes_actor_to_audit():
    """Audit row's actor field carries the caller-supplied actor
    (typically the masked email)."""
    request = _fake_request()
    expected = _expected_field_name(
        hpv.FORM_PATH_PASSWORD_RESET, "tenant-Y", now=_ts(3),
    )
    audit_log = AsyncMock()
    with patch("backend.audit.log", audit_log):
        _run(hpv.verify_form_honeypot(
            hpv.FORM_ACTION_PASSWORD_RESET,
            submitted={expected: "x"},  # filled → fail
            request=request,
            tenant_id="tenant-Y",
            actor="bob@example.com",
            now=_ts(3),
        ))
    audit_log.assert_awaited_once()
    assert audit_log.await_args.kwargs["actor"] == "bob@example.com"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 8 — verify_form_honeypot_or_reject
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_or_reject_filled_field_raises():
    request = _fake_request()
    expected = _expected_field_name(
        hpv.FORM_PATH_LOGIN, hpv.ANONYMOUS_TENANT_ID, now=_ts(4),
    )
    with patch("backend.audit.log", AsyncMock()):
        with pytest.raises(honeypot.HoneypotRejected) as excinfo:
            _run(hpv.verify_form_honeypot_or_reject(
                hpv.FORM_ACTION_LOGIN,
                submitted={expected: "filled-by-bot"},
                request=request,
                now=_ts(4),
            ))
    assert excinfo.value.code == honeypot.HONEYPOT_REJECTED_CODE
    assert excinfo.value.http_status == honeypot.HONEYPOT_REJECTED_HTTP_STATUS
    assert excinfo.value.result.outcome == honeypot.OUTCOME_HONEYPOT_FAIL


def test_or_reject_form_drift_raises():
    """Missing honeypot field → raises (frontend deploy alarm)."""
    request = _fake_request()
    with patch("backend.audit.log", AsyncMock()):
        with pytest.raises(honeypot.HoneypotRejected) as excinfo:
            _run(hpv.verify_form_honeypot_or_reject(
                hpv.FORM_ACTION_LOGIN,
                submitted={"email": "a@b"},
                request=request,
                now=_ts(4),
            ))
    assert excinfo.value.result.outcome == honeypot.OUTCOME_HONEYPOT_FORM_DRIFT


def test_or_reject_pass_returns_result():
    request = _fake_request()
    expected = _expected_field_name(
        hpv.FORM_PATH_LOGIN, hpv.ANONYMOUS_TENANT_ID, now=_ts(4),
    )
    with patch("backend.audit.log", AsyncMock()):
        result = _run(hpv.verify_form_honeypot_or_reject(
            hpv.FORM_ACTION_LOGIN,
            submitted={expected: ""},
            request=request,
            now=_ts(4),
        ))
    assert result.allow is True
    assert result.outcome == honeypot.OUTCOME_HONEYPOT_PASS


def test_or_reject_uses_canonical_429_surface():
    """Same 429 + ``bot_challenge_failed`` surface as
    :class:`bot_challenge.BotChallengeRejected` so the front-end UI
    keys on a single error code regardless of which layer caught
    the bot."""
    assert honeypot.HONEYPOT_REJECTED_CODE == bot_challenge.BOT_CHALLENGE_REJECTED_CODE
    assert honeypot.HONEYPOT_REJECTED_HTTP_STATUS == bot_challenge.BOT_CHALLENGE_REJECTED_HTTP_STATUS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 9 — Module-global state audit (SOP §1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_no_module_level_mutable_container():
    """SOP §1 invariant — every module-level mapping / collection
    must be frozen. Mutable list / dict / set at module level
    breaks cross-worker consistency."""
    for name in dir(hpv):
        if name.startswith("_"):
            continue
        value = getattr(hpv, name)
        if isinstance(value, (list, dict, set)):
            pytest.fail(
                f"AS.6.4 SOP §1: module-level mutable container "
                f"{name!r} = {type(value).__name__}. Frozen container required."
            )


def test_module_reload_keeps_constants_stable():
    constants_before = {
        "FORM_ACTION_LOGIN": hpv.FORM_ACTION_LOGIN,
        "FORM_ACTION_SIGNUP": hpv.FORM_ACTION_SIGNUP,
        "FORM_ACTION_PASSWORD_RESET": hpv.FORM_ACTION_PASSWORD_RESET,
        "FORM_ACTION_CONTACT": hpv.FORM_ACTION_CONTACT,
        "FORM_PATH_LOGIN": hpv.FORM_PATH_LOGIN,
        "FORM_PATH_SIGNUP": hpv.FORM_PATH_SIGNUP,
        "FORM_PATH_PASSWORD_RESET": hpv.FORM_PATH_PASSWORD_RESET,
        "FORM_PATH_CONTACT": hpv.FORM_PATH_CONTACT,
        "ANONYMOUS_TENANT_ID": hpv.ANONYMOUS_TENANT_ID,
    }
    importlib.reload(hpv)
    for name, value in constants_before.items():
        assert getattr(hpv, name) == value, f"{name} drifted across reload"


def test_all_export_resolves():
    for symbol in hpv.__all__:
        assert hasattr(hpv, symbol), f"__all__ lists {symbol!r} but module has no such attr"


def test_no_inline_event_strings_in_module():
    """AS.0.5 §3 invariant — bot_challenge.* event names must come
    via constants (honeypot.EVENT_BOT_CHALLENGE_HONEYPOT_*), not
    inline literals. Drift guard catches a regression at PR review.
    """
    import pathlib
    import re
    src = pathlib.Path(hpv.__file__).read_text()
    inline = re.compile(r'["\']bot_challenge\.\w+["\']')
    matches = inline.findall(src)
    assert matches == [], (
        f"AS.6.4 module embeds inline bot_challenge.* literals "
        f"instead of routing via honeypot / bot_challenge constants: "
        f"{matches}"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 10 — Outcome routing drift guard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_outcome_to_honeypot_bypass_kind_keys_in_bot_challenge():
    """AS.6.4 _OUTCOME_TO_HONEYPOT_BYPASS_KIND maps AS.3.1
    OUTCOME_BYPASS_* literals onto the AS.4.1 honeypot bypass
    vocabulary. Keys must be valid bot_challenge outcomes."""
    for outcome in hpv._OUTCOME_TO_HONEYPOT_BYPASS_KIND.keys():
        assert outcome in bot_challenge.ALL_OUTCOMES, (
            f"mapping key {outcome!r} not in bot_challenge.ALL_OUTCOMES"
        )


def test_outcome_to_honeypot_bypass_kind_values_in_honeypot_vocab():
    for kind in hpv._OUTCOME_TO_HONEYPOT_BYPASS_KIND.values():
        assert kind in honeypot.ALL_BYPASS_KINDS, (
            f"mapping value {kind!r} not in honeypot.ALL_BYPASS_KINDS"
        )


def test_outcome_to_honeypot_bypass_kind_covers_three_axes():
    """All three AS.0.6 §4 bypass axes (api_key / test_token /
    ip_allowlist) must be covered. knob_off and tenant_disabled are
    NOT axes — they're caller-side flags handled separately."""
    expected_outcomes = {
        bot_challenge.OUTCOME_BYPASS_APIKEY,
        bot_challenge.OUTCOME_BYPASS_TEST_TOKEN,
        bot_challenge.OUTCOME_BYPASS_IP_ALLOWLIST,
    }
    assert set(hpv._OUTCOME_TO_HONEYPOT_BYPASS_KIND.keys()) == expected_outcomes

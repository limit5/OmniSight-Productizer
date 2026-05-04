"""AS.6.5 — Unit tests for ``backend.security.auth_audit_bridge``.

Coverage shape (test families):

  * Family 1  — Module surface: 13 ``__all__`` exports present + no
                stowaway + no module-level mutable container + module
                reload keeps constants stable.
  * Family 2  — MFA method vocabulary pinning: 3 string constants byte-
                equal the OmniSight MFA challenge handler labels;
                :data:`SUPPORTED_MFA_METHODS` frozenset shape.
  * Family 3  — :func:`mfa_method_to_auth_method` dispatch: 3 known
                mappings + ValueError on unknown + value-half drift
                guard against :data:`auth_event.AUTH_METHODS`.
  * Family 4  — :func:`request_client_ip` + :func:`request_user_agent`:
                CF-Connecting-IP precedence over ``request.client.host``,
                whitespace strip, None-on-missing, None-on-no-request,
                shim-friendly (objects without ``.headers`` don't raise).
  * Family 5  — :func:`emit_login_success_event`: routes through
                AS.5.1 ``emit_login_success`` with the right Context
                shape; default ``auth_method=password`` + actor fallback
                to user_id; CF / UA propagation.
  * Family 6  — :func:`emit_login_fail_event`: routes through AS.5.1
                ``emit_login_fail``; ``actor="anonymous"`` default;
                fail_reason vocabulary forwarded; raw attempted_user
                forwarded (the AS.5.1 builder owns the fingerprint).
  * Family 7  — :func:`emit_token_refresh_event`: routes through
                AS.5.1 ``emit_token_refresh``; outcome propagation;
                actor fallback.
  * Family 8  — :func:`emit_token_rotated_event`: routes through
                AS.5.1 ``emit_token_rotated``; triggered_by + tokens
                propagation.
  * Family 9  — Knob-off: every 4 emit_* returns ``None`` without
                touching the AS.5.1 layer when ``auth_event.is_enabled``
                returns False.
  * Family 10 — Failure swallow: when the underlying emit_* raises,
                the bridge returns ``None`` instead of propagating.
  * Family 11 — Schedule helpers: fire-and-forget; no ``coroutine
                was never awaited`` warning when called outside a
                running event loop (sync context); successful schedule
                from inside an asyncio loop dispatches the coro.

Module-global state audit (per implement_phase_step.md SOP §1)
* Tests are self-contained; no shared mutable state.
* Each emit test patches ``auth_event.emit_*`` per-call via
  ``unittest.mock.patch`` so no audit chain row is written.
* ``importlib.reload`` round-trip in family 1 confirms idempotency.
"""

from __future__ import annotations

import asyncio
import importlib
import warnings
from types import MappingProxyType
from unittest.mock import patch

import pytest

from backend.security import auth_audit_bridge as bridge
from backend.security import auth_event as ae


# ──────────────────────────────────────────────────────
#  Test helpers
# ──────────────────────────────────────────────────────


class _FakeClient:
    def __init__(self, host: str | None) -> None:
        self.host = host


class _FakeRequest:
    """Minimal stand-in for FastAPI's Request shape.

    Used everywhere a real Request is unnecessary; only ``headers``
    + ``client.host`` are read by the bridge extraction helpers.
    """

    def __init__(
        self,
        *,
        ua: str | None = "UA/1.0",
        cf_ip: str | None = None,
        host: str | None = "127.0.0.1",
    ) -> None:
        self.headers: dict[str, str] = {}
        if ua is not None:
            self.headers["user-agent"] = ua
        if cf_ip is not None:
            self.headers["cf-connecting-ip"] = cf_ip
        self.client = _FakeClient(host)


def _run(coro):
    """Drive an async coroutine in a fresh loop.

    ``asyncio.get_event_loop()`` is deprecated when no current loop
    exists in the thread; using ``new_event_loop`` per call also
    insulates these unit tests from cross-test loop pollution
    (some siblings install + close loops which would otherwise
    leave ``get_event_loop()`` returning a closed loop).
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 1 — Module surface + reload
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_all_exports_resolve() -> None:
    for name in bridge.__all__:
        assert hasattr(bridge, name), f"missing export {name!r}"


def test_all_exports_count() -> None:
    # 13 = 4 MFA constants + 1 SUPPORTED_MFA_METHODS frozenset +
    # 1 mfa_method_to_auth_method + 2 request extractors + 4 async
    # emitters + 2 schedule helpers - 1 (because constants count as 4 not 5).
    # Concretely: MFA_METHOD_TOTP, MFA_METHOD_BACKUP_CODE,
    # MFA_METHOD_WEBAUTHN, SUPPORTED_MFA_METHODS, mfa_method_to_auth_method,
    # request_client_ip, request_user_agent, emit_login_success_event,
    # emit_login_fail_event, emit_token_refresh_event,
    # emit_token_rotated_event, schedule_login_success_event,
    # schedule_login_fail_event = 13.
    assert len(bridge.__all__) == 13


def test_no_stowaway_public_callables() -> None:
    """Every non-underscore module attr should appear in __all__ or
    be a typing/import helper. Pins the public surface so a future
    contributor can't accidentally widen it without updating __all__."""
    public_attrs = {
        n for n in dir(bridge)
        if not n.startswith("_")
        and n not in {"annotations", "asyncio", "logging", "MappingProxyType",
                      "Any", "Mapping", "Optional", "logger"}
    }
    extras = public_attrs - set(bridge.__all__)
    assert not extras, f"undeclared public attrs: {sorted(extras)}"


def test_module_reload_keeps_constants_stable() -> None:
    """Module reload must not re-derive constants — they're frozen."""
    pre_supported = bridge.SUPPORTED_MFA_METHODS
    pre_totp = bridge.MFA_METHOD_TOTP
    importlib.reload(bridge)
    assert bridge.SUPPORTED_MFA_METHODS == pre_supported
    assert bridge.MFA_METHOD_TOTP == pre_totp


def test_no_module_level_mutable_container() -> None:
    """Per SOP §1 — no list / dict / set at module top.

    MappingProxyType / frozenset are immutable and OK.  Pure functions
    are OK.  Anything else is a red flag for cross-worker drift.
    """
    for name in dir(bridge):
        if name.startswith("_"):
            continue
        attr = getattr(bridge, name)
        if isinstance(attr, (list, dict, set)):
            pytest.fail(
                f"mutable module-level container at {name!r}: "
                f"{type(attr).__name__}"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 2 — MFA method vocabulary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_mfa_method_constants_pinned() -> None:
    assert bridge.MFA_METHOD_TOTP == "totp"
    assert bridge.MFA_METHOD_BACKUP_CODE == "backup_code"
    assert bridge.MFA_METHOD_WEBAUTHN == "webauthn"


def test_supported_mfa_methods_frozenset() -> None:
    assert isinstance(bridge.SUPPORTED_MFA_METHODS, frozenset)
    assert bridge.SUPPORTED_MFA_METHODS == {
        "totp", "backup_code", "webauthn",
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 3 — mfa_method_to_auth_method dispatch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_mfa_method_dispatch_known_values() -> None:
    assert bridge.mfa_method_to_auth_method("totp") == ae.AUTH_METHOD_MFA_TOTP
    assert bridge.mfa_method_to_auth_method("backup_code") == ae.AUTH_METHOD_MFA_TOTP
    assert bridge.mfa_method_to_auth_method("webauthn") == ae.AUTH_METHOD_MFA_WEBAUTHN


def test_mfa_method_dispatch_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown MFA method"):
        bridge.mfa_method_to_auth_method("garbage")


def test_mfa_method_dispatch_values_in_auth_methods_vocab() -> None:
    """Drift guard: every value must be a valid AS.5.1 auth_method."""
    for label in bridge.SUPPORTED_MFA_METHODS:
        mapped = bridge.mfa_method_to_auth_method(label)
        assert mapped in ae.AUTH_METHODS, (
            f"mfa label {label!r} → {mapped!r} not in AS.5.1 AUTH_METHODS "
            f"vocabulary {sorted(ae.AUTH_METHODS)}"
        )


def test_mfa_method_dispatch_table_is_immutable() -> None:
    """Internal table must be MappingProxyType so a bug elsewhere
    can't accidentally mutate the routing at runtime."""
    table = bridge._build_mfa_method_to_auth_method()
    assert isinstance(table, MappingProxyType)
    with pytest.raises(TypeError):
        table["evil"] = "rogue"  # type: ignore[index]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 4 — Request extraction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_request_client_ip_cf_precedence() -> None:
    req = _FakeRequest(cf_ip="9.9.9.9", host="1.2.3.4")
    assert bridge.request_client_ip(req) == "9.9.9.9"


def test_request_client_ip_falls_back_to_client_host() -> None:
    req = _FakeRequest(host="1.2.3.4")
    assert bridge.request_client_ip(req) == "1.2.3.4"


def test_request_client_ip_strips_whitespace_in_cf_header() -> None:
    req = _FakeRequest(cf_ip="  9.9.9.9  ", host="1.2.3.4")
    assert bridge.request_client_ip(req) == "9.9.9.9"


def test_request_client_ip_empty_cf_falls_back_to_client() -> None:
    req = _FakeRequest(cf_ip="   ", host="1.2.3.4")
    assert bridge.request_client_ip(req) == "1.2.3.4"


def test_request_client_ip_none_request_returns_none() -> None:
    assert bridge.request_client_ip(None) is None


def test_request_client_ip_missing_client_host_returns_none() -> None:
    class _NoClient:
        headers: dict[str, str] = {}
        client = None
    assert bridge.request_client_ip(_NoClient()) is None


def test_request_client_ip_request_without_headers_attr_swallows() -> None:
    """Defensive: a Request shim variant without ``.headers`` should
    not raise — extraction returns None."""
    class _BadReq:
        client = None
    # Will hit AttributeError on ``.headers`` access — bridge swallows.
    assert bridge.request_client_ip(_BadReq()) is None


def test_request_user_agent_simple() -> None:
    req = _FakeRequest(ua="Mozilla/5.0")
    assert bridge.request_user_agent(req) == "Mozilla/5.0"


def test_request_user_agent_missing_returns_none() -> None:
    req = _FakeRequest(ua=None)
    assert bridge.request_user_agent(req) is None


def test_request_user_agent_none_request() -> None:
    assert bridge.request_user_agent(None) is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 5 — emit_login_success_event
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_emit_login_success_routes_to_auth_event() -> None:
    """Bridge calls ``auth_event.emit_login_success`` with the right
    Context built from the kwargs."""
    captured: list[ae.LoginSuccessContext] = []

    async def fake_emit(ctx):
        captured.append(ctx)
        return 7

    req = _FakeRequest(cf_ip="9.9.9.9", ua="UA/1.0")
    with patch.object(ae, "emit_login_success", new=fake_emit):
        rid = _run(bridge.emit_login_success_event(
            user_id="u-test",
            request=req,
            auth_method=ae.AUTH_METHOD_PASSWORD,
            actor="user@example.com",
        ))
    assert rid == 7
    assert len(captured) == 1
    ctx = captured[0]
    assert ctx.user_id == "u-test"
    assert ctx.auth_method == ae.AUTH_METHOD_PASSWORD
    assert ctx.actor == "user@example.com"
    assert ctx.ip == "9.9.9.9"
    assert ctx.user_agent == "UA/1.0"
    assert ctx.mfa_satisfied is False
    assert ctx.provider is None


def test_emit_login_success_default_auth_method_is_password() -> None:
    captured: list[ae.LoginSuccessContext] = []

    async def fake_emit(ctx):
        captured.append(ctx)
        return None

    with patch.object(ae, "emit_login_success", new=fake_emit):
        _run(bridge.emit_login_success_event(user_id="u"))
    assert captured[0].auth_method == ae.AUTH_METHOD_PASSWORD


def test_emit_login_success_actor_fallback_to_user_id() -> None:
    captured: list[ae.LoginSuccessContext] = []

    async def fake_emit(ctx):
        captured.append(ctx)
        return None

    with patch.object(ae, "emit_login_success", new=fake_emit):
        _run(bridge.emit_login_success_event(user_id="u-abc"))
    assert captured[0].actor == "u-abc"


def test_emit_login_success_mfa_satisfied_propagates() -> None:
    captured: list[ae.LoginSuccessContext] = []

    async def fake_emit(ctx):
        captured.append(ctx)
        return None

    with patch.object(ae, "emit_login_success", new=fake_emit):
        _run(bridge.emit_login_success_event(
            user_id="u",
            auth_method=ae.AUTH_METHOD_MFA_TOTP,
            mfa_satisfied=True,
        ))
    assert captured[0].mfa_satisfied is True
    assert captured[0].auth_method == ae.AUTH_METHOD_MFA_TOTP


def test_emit_login_success_no_request_leaves_ip_ua_none() -> None:
    captured: list[ae.LoginSuccessContext] = []

    async def fake_emit(ctx):
        captured.append(ctx)
        return None

    with patch.object(ae, "emit_login_success", new=fake_emit):
        _run(bridge.emit_login_success_event(user_id="u"))
    assert captured[0].ip is None
    assert captured[0].user_agent is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 6 — emit_login_fail_event
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_emit_login_fail_routes_to_auth_event() -> None:
    captured: list[ae.LoginFailContext] = []

    async def fake_emit(ctx):
        captured.append(ctx)
        return 11

    req = _FakeRequest(cf_ip="9.9.9.9")
    with patch.object(ae, "emit_login_fail", new=fake_emit):
        rid = _run(bridge.emit_login_fail_event(
            attempted_user="victim@example.com",
            fail_reason=ae.LOGIN_FAIL_BAD_PASSWORD,
            request=req,
        ))
    assert rid == 11
    ctx = captured[0]
    assert ctx.attempted_user == "victim@example.com"
    assert ctx.fail_reason == ae.LOGIN_FAIL_BAD_PASSWORD
    assert ctx.actor == "anonymous"  # default
    assert ctx.auth_method == ae.AUTH_METHOD_PASSWORD  # default
    assert ctx.ip == "9.9.9.9"


def test_emit_login_fail_actor_explicit_overrides_anonymous() -> None:
    captured: list[ae.LoginFailContext] = []

    async def fake_emit(ctx):
        captured.append(ctx)
        return None

    with patch.object(ae, "emit_login_fail", new=fake_emit):
        _run(bridge.emit_login_fail_event(
            attempted_user="u@e.com",
            fail_reason=ae.LOGIN_FAIL_MFA_REQUIRED,
            actor="u@e.com",
        ))
    assert captured[0].actor == "u@e.com"


def test_emit_login_fail_each_vocab_reason_propagates() -> None:
    """Drift guard: every AS.5.1 fail_reason should round-trip
    through the bridge unmodified."""
    for reason in ae.LOGIN_FAIL_REASONS:
        captured: list[ae.LoginFailContext] = []

        async def fake_emit(ctx, c=captured):
            c.append(ctx)
            return None

        with patch.object(ae, "emit_login_fail", new=fake_emit):
            _run(bridge.emit_login_fail_event(
                attempted_user="x@y.com", fail_reason=reason,
            ))
        assert captured[0].fail_reason == reason


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 7 — emit_token_refresh_event
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_emit_token_refresh_routes_to_auth_event() -> None:
    captured: list[ae.TokenRefreshContext] = []

    async def fake_emit(ctx):
        captured.append(ctx)
        return 21

    with patch.object(ae, "emit_token_refresh", new=fake_emit):
        rid = _run(bridge.emit_token_refresh_event(
            user_id="u-1",
            provider="github",
            outcome=ae.TOKEN_REFRESH_SUCCESS,
            new_expires_in_seconds=3600,
        ))
    assert rid == 21
    ctx = captured[0]
    assert ctx.user_id == "u-1"
    assert ctx.provider == "github"
    assert ctx.outcome == ae.TOKEN_REFRESH_SUCCESS
    assert ctx.new_expires_in_seconds == 3600
    assert ctx.actor == "u-1"  # actor default = user_id


def test_emit_token_refresh_each_vocab_outcome_propagates() -> None:
    for outcome in ae.TOKEN_REFRESH_OUTCOMES:
        captured: list[ae.TokenRefreshContext] = []

        async def fake_emit(ctx, c=captured):
            c.append(ctx)
            return None

        with patch.object(ae, "emit_token_refresh", new=fake_emit):
            _run(bridge.emit_token_refresh_event(
                user_id="u", provider="github", outcome=outcome,
            ))
        assert captured[0].outcome == outcome


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 8 — emit_token_rotated_event
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_emit_token_rotated_routes_to_auth_event() -> None:
    captured: list[ae.TokenRotatedContext] = []

    async def fake_emit(ctx):
        captured.append(ctx)
        return 31

    with patch.object(ae, "emit_token_rotated", new=fake_emit):
        rid = _run(bridge.emit_token_rotated_event(
            user_id="u-1",
            provider="github",
            previous_refresh_token="old-refresh",
            new_refresh_token="new-refresh",
            triggered_by=ae.TOKEN_ROTATION_TRIGGER_AUTO,
        ))
    assert rid == 31
    ctx = captured[0]
    assert ctx.user_id == "u-1"
    assert ctx.previous_refresh_token == "old-refresh"
    assert ctx.new_refresh_token == "new-refresh"
    assert ctx.triggered_by == ae.TOKEN_ROTATION_TRIGGER_AUTO
    assert ctx.actor == "u-1"  # actor default


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 9 — Knob-off short-circuits everywhere
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_emit_login_success_knob_off_returns_none() -> None:
    """When AS.0.8 knob is off, the underlying emit returns None;
    the bridge surfaces that None back to the caller."""
    with patch.object(ae, "is_enabled", return_value=False):
        rid = _run(bridge.emit_login_success_event(user_id="u"))
    assert rid is None


def test_emit_login_fail_knob_off_returns_none() -> None:
    with patch.object(ae, "is_enabled", return_value=False):
        rid = _run(bridge.emit_login_fail_event(
            attempted_user="x", fail_reason=ae.LOGIN_FAIL_BAD_PASSWORD,
        ))
    assert rid is None


def test_emit_token_refresh_knob_off_returns_none() -> None:
    with patch.object(ae, "is_enabled", return_value=False):
        rid = _run(bridge.emit_token_refresh_event(
            user_id="u", provider="github",
            outcome=ae.TOKEN_REFRESH_SUCCESS,
        ))
    assert rid is None


def test_emit_token_rotated_knob_off_returns_none() -> None:
    with patch.object(ae, "is_enabled", return_value=False):
        rid = _run(bridge.emit_token_rotated_event(
            user_id="u", provider="github",
            previous_refresh_token="old", new_refresh_token="new",
            triggered_by=ae.TOKEN_ROTATION_TRIGGER_AUTO,
        ))
    assert rid is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 10 — Failure swallow
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_emit_login_success_swallows_underlying_exception() -> None:
    async def boom(ctx):
        raise RuntimeError("audit chain unavailable")

    with patch.object(ae, "emit_login_success", new=boom):
        rid = _run(bridge.emit_login_success_event(user_id="u"))
    assert rid is None


def test_emit_login_fail_swallows_underlying_exception() -> None:
    async def boom(ctx):
        raise RuntimeError("nope")

    with patch.object(ae, "emit_login_fail", new=boom):
        rid = _run(bridge.emit_login_fail_event(
            attempted_user="x", fail_reason=ae.LOGIN_FAIL_BAD_PASSWORD,
        ))
    assert rid is None


def test_emit_token_refresh_swallows_underlying_exception() -> None:
    async def boom(ctx):
        raise RuntimeError("nope")

    with patch.object(ae, "emit_token_refresh", new=boom):
        rid = _run(bridge.emit_token_refresh_event(
            user_id="u", provider="github", outcome=ae.TOKEN_REFRESH_SUCCESS,
        ))
    assert rid is None


def test_emit_token_rotated_swallows_underlying_exception() -> None:
    async def boom(ctx):
        raise RuntimeError("nope")

    with patch.object(ae, "emit_token_rotated", new=boom):
        rid = _run(bridge.emit_token_rotated_event(
            user_id="u", provider="github",
            previous_refresh_token="o", new_refresh_token="n",
            triggered_by=ae.TOKEN_ROTATION_TRIGGER_AUTO,
        ))
    assert rid is None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Family 11 — Schedule helpers (fire-and-forget)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_schedule_login_success_no_running_loop_does_not_warn() -> None:
    """Sync context (no running event loop): schedule should swallow
    cleanly without emitting an "unawaited coroutine" warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        # No event loop running here — must not raise the warning-as-error.
        bridge.schedule_login_success_event(
            user_id="u", request=_FakeRequest(),
        )


def test_schedule_login_fail_no_running_loop_does_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        bridge.schedule_login_fail_event(
            attempted_user="x", fail_reason=ae.LOGIN_FAIL_BAD_PASSWORD,
        )


def test_schedule_login_success_inside_running_loop_dispatches() -> None:
    """Inside an asyncio loop, schedule should dispatch the coroutine
    via ensure_future. We verify by mocking emit and waiting one tick."""
    captured: list[ae.LoginSuccessContext] = []

    async def fake_emit(ctx):
        captured.append(ctx)
        return None

    async def driver():
        with patch.object(ae, "emit_login_success", new=fake_emit):
            bridge.schedule_login_success_event(user_id="u")
            # Yield once so the scheduled task can run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    _run(driver())
    assert len(captured) == 1
    assert captured[0].user_id == "u"


def test_schedule_login_fail_inside_running_loop_dispatches() -> None:
    captured: list[ae.LoginFailContext] = []

    async def fake_emit(ctx):
        captured.append(ctx)
        return None

    async def driver():
        with patch.object(ae, "emit_login_fail", new=fake_emit):
            bridge.schedule_login_fail_event(
                attempted_user="x", fail_reason=ae.LOGIN_FAIL_BAD_PASSWORD,
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    _run(driver())
    assert len(captured) == 1
    assert captured[0].fail_reason == ae.LOGIN_FAIL_BAD_PASSWORD

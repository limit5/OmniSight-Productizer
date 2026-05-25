"""OP-1730 — runtime honeypot field-name config endpoint.

Pins the public ``GET /auth/honeypot-field-config`` contract that lets the
auth pages render the rotating AS.6.4 honeypot field on a NON-secure-context
origin (plain HTTP / bare IP), where ``crypto.subtle`` is undefined and the
client-side Web Crypto field-name derivation throws. The endpoint returns the
server-derived current(+previous-epoch) field name(s) so the hidden input
round-trips and ``validate_honeypot`` accepts the submit.

The handler is exercised directly with a minimal ASGI ``Request`` — the same
no-FastAPI-machinery style as ``test_bot_challenge_config_endpoint.py`` — so
the test stays fast and free of DB / pool wiring.
"""

from __future__ import annotations

import json

import pytest
from starlette.requests import Request

from backend.routers import auth as auth_router
from backend.security import honeypot as hp
from backend.security import honeypot_form_verifier as hpv


def _fake_request() -> Request:
    scope = {
        "type": "http",
        "headers": [],
        "client": ("203.0.113.7", 0),
    }
    return Request(scope)


async def _call(form: str = "login") -> dict:
    resp = await auth_router.honeypot_field_config(_fake_request(), form=form)
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_login_field_name_equals_backend_current_epoch() -> None:
    """The served current-epoch name is byte-equal to the backend's own
    ``honeypot_field_name`` — i.e. exactly what ``validate_honeypot``
    will accept for an anonymous login form."""
    payload = await _call("login")
    name_now, name_prev = hp.expected_field_names(
        hpv.FORM_PATH_LOGIN, hpv.ANONYMOUS_TENANT_ID
    )
    assert payload["form_path"] == hpv.FORM_PATH_LOGIN
    assert payload["field_name"] == name_now
    assert payload["field_names"] == [name_now, name_prev]
    # Sanity: the login prefix is present (anti-typo guard on the wiring).
    assert payload["field_name"].startswith("lg_")


@pytest.mark.asyncio
async def test_served_name_round_trips_through_validate_honeypot() -> None:
    """A form carrying ONLY the served field name (empty value) passes the
    backend honeypot validator — the end-to-end contract the fallback relies
    on. This is the AC 'field name resolves AND equals the backend's
    current-epoch name' exercised through the real validator."""
    payload = await _call("login")
    result = hp.validate_honeypot(
        hpv.FORM_PATH_LOGIN,
        hpv.ANONYMOUS_TENANT_ID,
        {payload["field_name"]: ""},
    )
    assert result.allow is True
    assert result.outcome == hp.OUTCOME_HONEYPOT_PASS


@pytest.mark.asyncio
async def test_unknown_form_falls_back_to_login() -> None:
    """An unrecognised ``form`` query value degrades to the login form
    rather than 500-ing the pre-auth page."""
    payload = await _call("not-a-real-form")
    assert payload["form_path"] == hpv.FORM_PATH_LOGIN


@pytest.mark.asyncio
async def test_signup_and_reset_forms_route_to_their_paths() -> None:
    """The endpoint serves the right field-name space for the other
    affected self-forms (signup / password-reset)."""
    signup = await _call("signup")
    assert signup["form_path"] == hpv.FORM_PATH_SIGNUP
    assert signup["field_name"].startswith("sg_")

    pwreset = await _call("pwreset")
    assert pwreset["form_path"] == hpv.FORM_PATH_PASSWORD_RESET
    assert pwreset["field_name"].startswith("pr_")


@pytest.mark.asyncio
async def test_enabled_mirrors_single_knob(monkeypatch) -> None:
    """``enabled`` reflects the AS.0.8 single knob; the field name is still
    served when the knob is off (the backend bypasses honeypot, so the
    rendered field is simply ignored — never a secret leaked)."""
    monkeypatch.setattr("backend.security.honeypot.is_enabled", lambda: False)
    payload = await _call("login")
    assert payload["enabled"] is False
    # Name is still present — exposing it is safe (SHA-256 over a
    # non-secret seed) and lets the field render either way.
    assert payload["field_name"].startswith("lg_")

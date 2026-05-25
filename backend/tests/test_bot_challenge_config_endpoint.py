"""OP-1726 — runtime bot-challenge config endpoint.

Pins the public ``GET /auth/bot-challenge-config`` contract that makes
the turnstile widget runtime-driven (one release image: prod ON when the
public ``NEXT_PUBLIC_*_SITE_KEY`` env is set, internal staging OFF when it
is not) instead of build-baked ``NEXT_PUBLIC`` reads.

The handler is exercised directly with a minimal ASGI ``Request`` — the
same no-FastAPI-machinery style as ``test_login_rate_limit.py`` — so the
test stays fast and free of DB / pool wiring.
"""

from __future__ import annotations

import json

import pytest
from starlette.requests import Request

from backend.auth_provisioning import bot_defense
from backend.routers import auth as auth_router
from backend.security import bot_challenge


def _fake_request(cf_ipcountry: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if cf_ipcountry:
        headers.append((b"cf-ipcountry", cf_ipcountry.encode()))
    scope = {
        "type": "http",
        "headers": headers,
        "client": ("203.0.113.7", 0),
    }
    return Request(scope)


async def _call(cf_ipcountry: str | None = None) -> dict:
    resp = await auth_router.bot_challenge_config(_fake_request(cf_ipcountry))
    return json.loads(resp.body)


# ── pure accessor (the _SITE_KEY_ENVS half of the derivation) ──


def test_site_key_env_for_maps_every_provider() -> None:
    assert (
        bot_defense.site_key_env_for(bot_challenge.Provider.TURNSTILE)
        == "NEXT_PUBLIC_TURNSTILE_SITE_KEY"
    )
    assert (
        bot_defense.site_key_env_for(bot_challenge.Provider.HCAPTCHA)
        == "NEXT_PUBLIC_HCAPTCHA_SITE_KEY"
    )
    # v2 + v3 share the reCAPTCHA public site-key env.
    assert (
        bot_defense.site_key_env_for(bot_challenge.Provider.RECAPTCHA_V2)
        == "NEXT_PUBLIC_RECAPTCHA_SITE_KEY"
    )
    assert (
        bot_defense.site_key_env_for(bot_challenge.Provider.RECAPTCHA_V3)
        == "NEXT_PUBLIC_RECAPTCHA_SITE_KEY"
    )


# ── endpoint: off / on / provider switch ──


@pytest.mark.asyncio
async def test_off_when_no_site_key_env(monkeypatch) -> None:
    """Internal-staging posture: no env → disabled, no key leaked."""
    monkeypatch.delenv("NEXT_PUBLIC_TURNSTILE_SITE_KEY", raising=False)
    payload = await _call()
    assert payload == {
        "provider": "turnstile",
        "site_key": None,
        "enabled": False,
    }


@pytest.mark.asyncio
async def test_on_when_site_key_env_set(monkeypatch) -> None:
    """Prod posture: env set → enabled, public site key served."""
    monkeypatch.setenv("NEXT_PUBLIC_TURNSTILE_SITE_KEY", "0xPROD_SITE_KEY")
    payload = await _call()
    assert payload["provider"] == "turnstile"
    assert payload["site_key"] == "0xPROD_SITE_KEY"
    assert payload["enabled"] is True


@pytest.mark.asyncio
async def test_disabled_when_as_family_off(monkeypatch) -> None:
    """Even with a key, the AS single-knob OFF disables the widget."""
    monkeypatch.setenv("NEXT_PUBLIC_TURNSTILE_SITE_KEY", "0xPROD_SITE_KEY")
    monkeypatch.setattr(
        "backend.security.turnstile_form_verifier.is_enabled",
        lambda: False,
    )
    payload = await _call()
    assert payload["enabled"] is False
    # No key leaked when disabled.
    assert payload["site_key"] is None


@pytest.mark.asyncio
async def test_provider_follows_region_heuristic(monkeypatch) -> None:
    """A GDPR-strict region routes to hCaptcha; its own env gates the key."""
    monkeypatch.delenv("NEXT_PUBLIC_TURNSTILE_SITE_KEY", raising=False)
    monkeypatch.setenv("NEXT_PUBLIC_HCAPTCHA_SITE_KEY", "0xHCAPTCHA_KEY")
    payload = await _call(cf_ipcountry="DE")
    assert payload["provider"] == "hcaptcha"
    assert payload["site_key"] == "0xHCAPTCHA_KEY"
    assert payload["enabled"] is True

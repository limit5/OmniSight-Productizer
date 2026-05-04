"""KS.3.13 -- BYOG single-knob registration contract tests."""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from backend import auth
from backend.security import cmek_revoke_detector as detector
from backend.security import cmek_wizard


TENANT = "t-acme"


@pytest.fixture(autouse=True)
def _reset_detector_and_knobs(monkeypatch):
    detector._reset_for_tests()
    monkeypatch.delenv(cmek_wizard.CMEK_ENABLED_ENV, raising=False)
    monkeypatch.delenv(cmek_wizard.BYOG_ENABLED_ENV, raising=False)
    yield
    detector._reset_for_tests()
    monkeypatch.delenv(cmek_wizard.CMEK_ENABLED_ENV, raising=False)
    monkeypatch.delenv(cmek_wizard.BYOG_ENABLED_ENV, raising=False)


def _actor() -> auth.User:
    return auth.User(
        id="u-admin",
        email="admin@example.com",
        name="Admin",
        role="super_admin",
    )


def test_byog_enabled_knob_defaults_true(monkeypatch) -> None:
    monkeypatch.delenv(cmek_wizard.BYOG_ENABLED_ENV, raising=False)

    assert cmek_wizard.is_byog_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off"])
def test_byog_enabled_knob_false_values(monkeypatch, raw: str) -> None:
    monkeypatch.setenv(cmek_wizard.BYOG_ENABLED_ENV, raw)

    assert cmek_wizard.is_byog_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "unexpected"])
def test_byog_enabled_knob_true_values(monkeypatch, raw: str) -> None:
    monkeypatch.setenv(cmek_wizard.BYOG_ENABLED_ENV, raw)

    assert cmek_wizard.is_byog_enabled() is True


@pytest.mark.asyncio
async def test_status_exposes_tier3_when_byog_enabled(monkeypatch) -> None:
    from backend.routers import cmek_wizard as router

    monkeypatch.setenv(cmek_wizard.BYOG_ENABLED_ENV, "true")

    response = await router.get_cmek_settings_status(TENANT, None, _actor())
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body["byog_enabled"] is True
    assert body["tier3_available"] is True
    assert body["proxy_mode_available"] is True
    assert body["available_security_tiers"] == ["tier-1", "tier-2", "tier-3"]


@pytest.mark.asyncio
async def test_status_hides_tier3_when_byog_disabled(monkeypatch) -> None:
    from backend.routers import cmek_wizard as router

    monkeypatch.setenv(cmek_wizard.BYOG_ENABLED_ENV, "false")

    response = await router.get_cmek_settings_status(TENANT, None, _actor())
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body["byog_enabled"] is False
    assert body["tier3_available"] is False
    assert body["proxy_mode_available"] is False
    assert body["available_security_tiers"] == ["tier-1", "tier-2"]


def test_source_fingerprint_clean():
    for path in [
        "backend/security/cmek_wizard.py",
        "backend/routers/cmek_wizard.py",
    ]:
        source = pathlib.Path(path).read_text()
        fingerprint = re.compile(
            r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
        )
        assert not fingerprint.search(source)

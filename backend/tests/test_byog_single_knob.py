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


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["true", "false"])
async def test_tier1_and_tier2_status_contract_unchanged_by_byog_knob(
    monkeypatch,
    raw: str,
) -> None:
    from backend.routers import cmek_wizard as router

    monkeypatch.setenv(cmek_wizard.BYOG_ENABLED_ENV, raw)
    detector.record_cmek_health_result(
        detector.CMEKHealthResult(
            tenant_id=TENANT,
            provider="aws-kms",
            key_id="arn:aws:kms:us-east-1:111122223333:key/example",
            ok=True,
            revoked=False,
            reason="enabled",
            checked_at=1.0,
            elapsed_ms=1.0,
            raw_state="Enabled",
            detail={},
        )
    )

    tier1 = json.loads((await router.get_cmek_settings_status("t-basic", None, _actor())).body)
    tier2 = json.loads((await router.get_cmek_settings_status(TENANT, None, _actor())).body)

    assert tier1["security_tier"] == "tier-1"
    assert tier1["tier2_available"] is True
    assert tier1["kms_health"] == "not_configured"
    assert tier1["revoke_status"] == "clear"
    assert tier2["security_tier"] == "tier-2"
    assert tier2["tier2_available"] is True
    assert tier2["kms_health"] == "healthy"
    assert tier2["revoke_status"] == "clear"


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

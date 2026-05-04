"""KS.2.12 -- CMEK single-knob rollback contract tests."""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from backend import auth
from backend.security import cmek_revoke_detector as detector
from backend.security import cmek_wizard


TENANT = "t-acme"
AWS_KEY_ID = (
    "arn:aws:kms:us-east-1:111122223333:key/"
    "00000000-0000-0000-0000-000000000000"
)


@pytest.fixture(autouse=True)
def _reset_detector_and_knob(monkeypatch):
    detector._reset_for_tests()
    monkeypatch.delenv(cmek_wizard.CMEK_ENABLED_ENV, raising=False)
    yield
    detector._reset_for_tests()
    monkeypatch.delenv(cmek_wizard.CMEK_ENABLED_ENV, raising=False)


def _actor() -> auth.User:
    return auth.User(
        id="u-admin",
        email="admin@example.com",
        name="Admin",
        role="super_admin",
    )


def _healthy_result() -> detector.CMEKHealthResult:
    return detector.CMEKHealthResult(
        tenant_id=TENANT,
        provider="aws-kms",
        key_id=AWS_KEY_ID,
        ok=True,
        revoked=False,
        reason="describe_ok",
        checked_at=3.0,
        elapsed_ms=2.0,
        raw_state="Enabled",
        detail={"key_state": "Enabled"},
    )


def test_cmek_enabled_knob_defaults_true(monkeypatch) -> None:
    monkeypatch.delenv(cmek_wizard.CMEK_ENABLED_ENV, raising=False)

    assert cmek_wizard.is_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off"])
def test_cmek_enabled_knob_false_values(monkeypatch, raw: str) -> None:
    monkeypatch.setenv(cmek_wizard.CMEK_ENABLED_ENV, raw)

    assert cmek_wizard.is_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "unexpected"])
def test_cmek_enabled_knob_true_values(monkeypatch, raw: str) -> None:
    monkeypatch.setenv(cmek_wizard.CMEK_ENABLED_ENV, raw)

    assert cmek_wizard.is_enabled() is True


@pytest.mark.asyncio
async def test_status_forces_tier1_fallback_when_cmek_disabled(monkeypatch) -> None:
    from backend.routers import cmek_wizard as router

    detector.record_cmek_health_result(_healthy_result())
    monkeypatch.setenv(cmek_wizard.CMEK_ENABLED_ENV, "false")

    response = await router.get_cmek_settings_status(TENANT, None, _actor())
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body["security_tier"] == "tier-1"
    assert body["previous_security_tier"] == "tier-2"
    assert body["kms_health"] == "cmek_disabled"
    assert body["revoke_status"] == "fallback_to_tier1"
    assert body["cmek_enabled"] is False
    assert body["tier2_available"] is False
    assert body["wizard_visible"] is False
    assert body["available_security_tiers"] == ["tier-1"]
    assert body["provider"] == ""
    assert body["key_id"] == ""


@pytest.mark.asyncio
async def test_tier2_wizard_is_hidden_when_cmek_disabled(monkeypatch) -> None:
    from backend.routers import cmek_wizard as router

    monkeypatch.setenv(cmek_wizard.CMEK_ENABLED_ENV, "false")

    response = await router.list_cmek_wizard_providers(TENANT, None, _actor())
    body = json.loads(response.body)

    assert response.status_code == 404
    assert body["error_code"] == "cmek_disabled"
    assert body["cmek_enabled"] is False
    assert body["security_tier"] == "tier-1"


@pytest.mark.asyncio
async def test_tier2_upgrade_is_blocked_when_cmek_disabled(monkeypatch) -> None:
    from backend.routers import cmek_wizard as router

    monkeypatch.setenv(cmek_wizard.CMEK_ENABLED_ENV, "false")

    response = await router.start_tier1_to_tier2_upgrade(
        TENANT,
        router.TierUpgradeRequest(
            provider="aws-kms",
            key_id=AWS_KEY_ID,
            dek_refs=[],
        ),
        None,
        _actor(),
    )
    body = json.loads(response.body)

    assert response.status_code == 404
    assert body["error_code"] == "cmek_disabled"
    assert body["retryable"] is False


@pytest.mark.asyncio
async def test_tier2_downgrade_stays_available_when_cmek_disabled(monkeypatch) -> None:
    from backend.routers import cmek_wizard as router

    class FakeResult:
        def to_dict(self):
            return {
                "downgrade_id": "cmekd_disabled",
                "tenant_id": TENANT,
                "from_security_tier": "tier-2",
                "to_security_tier": "tier-1",
                "status": "completed",
                "progress_percent": 100,
                "customer_iam_dependency": "required-until-downgrade-persisted",
            }

    def fake_plan(**kwargs):
        assert kwargs["tenant_id"] == TENANT
        assert kwargs["provider"] == "aws-kms"
        assert kwargs["key_id"] == AWS_KEY_ID
        assert kwargs["dek_refs"] == []
        return FakeResult()

    monkeypatch.setenv(cmek_wizard.CMEK_ENABLED_ENV, "false")
    monkeypatch.setattr(
        router._cmek_upgrade,
        "plan_tier2_to_tier1_downgrade",
        fake_plan,
    )

    response = await router.start_tier2_to_tier1_downgrade(
        TENANT,
        router.TierDowngradeRequest(
            provider="aws-kms",
            key_id=AWS_KEY_ID,
            dek_refs=[],
        ),
        None,
        _actor(),
    )
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body["downgrade_id"] == "cmekd_disabled"
    assert body["to_security_tier"] == "tier-1"
    assert body["customer_iam_dependency"] == "required-until-downgrade-persisted"


def test_detector_env_loader_returns_no_checks_when_cmek_disabled(monkeypatch):
    monkeypatch.setenv(cmek_wizard.CMEK_ENABLED_ENV, "false")
    monkeypatch.setenv("OMNISIGHT_CMEK_HEALTH_TENANT_ID", TENANT)
    monkeypatch.setenv("OMNISIGHT_AWS_KMS_KEY_ID", AWS_KEY_ID)
    monkeypatch.setenv("OMNISIGHT_AWS_KMS_REGION", "us-east-1")

    assert detector.load_env_cmek_key_checks() == []


def test_source_fingerprint_clean():
    for path in [
        "backend/security/cmek_wizard.py",
        "backend/security/cmek_revoke_detector.py",
        "backend/routers/cmek_wizard.py",
    ]:
        source = pathlib.Path(path).read_text()
        fingerprint = re.compile(
            r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
        )
        assert not fingerprint.search(source)

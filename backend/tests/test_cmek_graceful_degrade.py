"""KS.2.6 -- CMEK revoke graceful-degrade contract tests."""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from backend import auth
from backend.security import cmek_graceful_degrade as degrade
from backend.security import cmek_revoke_detector as detector


RUNBOOK = pathlib.Path("docs/ops/cmek_revoke_recovery.md")


@pytest.fixture(autouse=True)
def _reset_detector():
    detector._reset_for_tests()
    yield
    detector._reset_for_tests()


def _revoked_result(*, checked_at: float = 1.0) -> detector.CMEKHealthResult:
    return detector.CMEKHealthResult(
        tenant_id="t-acme",
        provider="aws-kms",
        key_id="arn:aws:kms:us-east-1:111122223333:key/demo",
        ok=False,
        revoked=True,
        reason="describe_failed",
        checked_at=checked_at,
        elapsed_ms=2.0,
        raw_state="KMSOperationError",
        detail={"error": "AccessDeniedException"},
    )


def test_degrade_decision_allows_tenant_without_revoked_snapshot():
    decision = degrade.cmek_degrade_decision_for_tenant(
        "t-acme",
        latest_results=[
            {
                "tenant_id": "t-acme",
                "revoked": False,
                "checked_at": 1.0,
            }
        ],
    )

    assert decision.allowed is True
    assert decision.retryable is False


def test_degrade_decision_returns_friendly_non_retryable_error_payload():
    detector.record_cmek_health_result(_revoked_result())

    decision = degrade.cmek_degrade_decision_for_tenant("t-acme")
    payload = decision.to_error_payload()

    assert decision.allowed is False
    assert payload["error_code"] == "cmek_revoked"
    assert payload["retryable"] is False
    assert payload["recovery_runbook"] == "docs/ops/cmek_revoke_recovery.md"
    assert "in-flight requests are allowed to finish" in payload["detail"]
    assert "applies only at request start" in payload["in_flight_policy"]
    assert payload["provider"] == "aws-kms"
    assert payload["reason"] == "describe_failed"


def test_degrade_decision_uses_latest_revoked_snapshot_for_tenant():
    old = _revoked_result(checked_at=1.0)
    new = detector.CMEKHealthResult(
        tenant_id="t-acme",
        provider="gcp-kms",
        key_id="projects/acme-prod/locations/us/keyRings/r/cryptoKeys/k",
        ok=False,
        revoked=True,
        reason="key_disabled",
        checked_at=2.0,
        elapsed_ms=1.0,
        raw_state="DISABLED",
        detail={"primary_state": "DISABLED"},
    )

    decision = degrade.cmek_degrade_decision_for_tenant(
        "t-acme",
        latest_results=[old.to_dict(), new.to_dict()],
    )

    assert decision.provider == "gcp-kms"
    assert decision.reason == "key_disabled"
    assert decision.checked_at == 2.0


@pytest.mark.asyncio
async def test_cmek_wizard_new_request_returns_403_without_retry_after():
    from backend.routers import cmek_wizard

    detector.record_cmek_health_result(_revoked_result())
    actor = auth.User(
        id="u-admin",
        email="admin@example.com",
        name="Admin",
        role="super_admin",
    )

    response = await cmek_wizard.list_cmek_wizard_providers("t-acme", None, actor)
    body = json.loads(response.body)

    assert response.status_code == 403
    assert "retry-after" not in {k.lower() for k in response.headers}
    assert body["error_code"] == "cmek_revoked"
    assert body["retryable"] is False
    assert body["recovery_runbook"] == "docs/ops/cmek_revoke_recovery.md"


@pytest.mark.asyncio
async def test_cmek_settings_status_reports_revoke_and_health_badges():
    from backend.routers import cmek_wizard

    detector.record_cmek_health_result(_revoked_result(checked_at=3.0))
    actor = auth.User(
        id="u-admin",
        email="admin@example.com",
        name="Admin",
        role="super_admin",
    )

    response = await cmek_wizard.get_cmek_settings_status("t-acme", None, actor)
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body["security_tier"] == "tier-2"
    assert body["kms_health"] == "revoked"
    assert body["revoke_status"] == "revoked"
    assert body["provider"] == "aws-kms"
    assert body["reason"] == "describe_failed"


@pytest.mark.asyncio
async def test_cmek_settings_status_defaults_to_tier1_without_health_snapshot():
    from backend.routers import cmek_wizard

    actor = auth.User(
        id="u-admin",
        email="admin@example.com",
        name="Admin",
        role="super_admin",
    )

    response = await cmek_wizard.get_cmek_settings_status("t-acme", None, actor)
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body["security_tier"] == "tier-1"
    assert body["kms_health"] == "not_configured"
    assert body["revoke_status"] == "clear"


def test_recovery_runbook_documents_no_retry_and_restore_flow():
    text = RUNBOOK.read_text(encoding="utf-8")

    assert "error_code" in text
    assert "cmek_revoked" in text
    assert "retryable" in text
    assert "false" in text
    assert "Do not retry the same request" in text
    assert "AWS KMS" in text
    assert "Google Cloud KMS" in text
    assert "Vault Transit" in text
    assert "in flight" in text or "in-flight" in text


def test_source_fingerprint_clean():
    source = pathlib.Path("backend/security/cmek_graceful_degrade.py").read_text()
    fingerprint = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    assert not fingerprint.search(source)

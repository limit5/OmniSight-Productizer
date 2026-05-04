"""KS.2.5 -- CMEK revoke detector contract tests."""

from __future__ import annotations

import asyncio
import pathlib
import re
from types import SimpleNamespace

import pytest

from backend.security import cmek_revoke_detector as detector
from backend.security import kms_adapters as kms


class _FakeAdapter:
    def __init__(self, *, provider: str, key_id: str, metadata):
        self.provider = provider
        self.key_id = key_id
        self.metadata = metadata
        self.describe_calls = 0

    def describe_key(self):
        self.describe_calls += 1
        if isinstance(self.metadata, Exception):
            raise self.metadata
        return self.metadata


@pytest.fixture(autouse=True)
def _reset_detector():
    detector._reset_for_tests()
    yield
    detector._reset_for_tests()


@pytest.mark.asyncio
async def test_aws_enabled_describe_key_is_healthy():
    adapter = _FakeAdapter(
        provider="aws-kms",
        key_id="arn:aws:kms:us-east-1:111122223333:key/demo",
        metadata={"KeyMetadata": {"KeyState": "Enabled"}},
    )

    result = await detector.check_cmek_key_health(
        detector.CMEKKeyCheck("t-acme", adapter)
    )

    assert result.ok is True
    assert result.revoked is False
    assert result.reason == "describe_ok"
    assert result.raw_state == "Enabled"
    assert adapter.describe_calls == 1


@pytest.mark.asyncio
async def test_aws_disabled_key_is_reported_as_revoked():
    adapter = _FakeAdapter(
        provider="aws-kms",
        key_id="arn:aws:kms:us-east-1:111122223333:key/demo",
        metadata={"KeyMetadata": {"KeyState": "Disabled"}},
    )

    result = await detector.check_cmek_key_health(
        detector.CMEKKeyCheck("t-acme", adapter)
    )

    assert result.ok is False
    assert result.revoked is True
    assert result.reason == "key_disabled"
    assert result.raw_state == "Disabled"
    assert result.detail == {"key_state": "Disabled"}


@pytest.mark.asyncio
async def test_describe_permission_failure_is_reported_as_revoked():
    adapter = _FakeAdapter(
        provider="aws-kms",
        key_id="arn:aws:kms:us-east-1:111122223333:key/demo",
        metadata=kms.KMSOperationError(
            "AccessDeniedException: not authorized to DescribeKey",
            provider="aws-kms",
            key_id="arn:aws:kms:us-east-1:111122223333:key/demo",
        ),
    )

    result = await detector.check_cmek_key_health(
        detector.CMEKKeyCheck("t-acme", adapter)
    )

    assert result.ok is False
    assert result.revoked is True
    assert result.reason == "describe_failed"
    assert "AccessDeniedException" in result.detail["error"]


@pytest.mark.asyncio
async def test_gcp_disabled_primary_version_is_reported_as_revoked():
    adapter = _FakeAdapter(
        provider="gcp-kms",
        key_id="projects/p/locations/global/keyRings/r/cryptoKeys/k",
        metadata=SimpleNamespace(primary=SimpleNamespace(state="DISABLED")),
    )

    result = await detector.check_cmek_key_health(
        detector.CMEKKeyCheck("t-acme", adapter)
    )

    assert result.ok is False
    assert result.revoked is True
    assert result.reason == "key_disabled"
    assert result.detail == {"primary_state": "DISABLED"}


@pytest.mark.asyncio
async def test_vault_transit_read_key_without_encrypt_support_is_revoked():
    adapter = _FakeAdapter(
        provider="vault-transit",
        key_id="tenant-dek",
        metadata={
            "data": {
                "supports_encryption": False,
                "supports_decryption": True,
            }
        },
    )

    result = await detector.check_cmek_key_health(
        detector.CMEKKeyCheck("t-acme", adapter)
    )

    assert result.ok is False
    assert result.revoked is True
    assert result.reason == "key_disabled"
    assert result.raw_state == "encrypt_decrypt_disabled"


def test_detection_interval_default_stays_inside_60_second_contract():
    assert detector.DEFAULT_INTERVAL_S <= detector.MAX_DETECTION_WINDOW_S
    assert detector.MAX_DETECTION_WINDOW_S == 60.0


def test_env_loader_uses_existing_aws_kms_prefix_without_sdk_import(monkeypatch):
    monkeypatch.setenv("OMNISIGHT_CMEK_HEALTH_TENANT_ID", "t-acme")
    monkeypatch.setenv(
        "OMNISIGHT_AWS_KMS_KEY_ID",
        "arn:aws:kms:us-east-1:111122223333:key/demo",
    )
    monkeypatch.setenv("OMNISIGHT_AWS_KMS_REGION", "us-east-1")

    checks = detector.load_env_cmek_key_checks()

    assert len(checks) == 1
    assert checks[0].tenant_id == "t-acme"
    assert checks[0].adapter.provider == "aws-kms"
    assert checks[0].adapter.key_id == "arn:aws:kms:us-east-1:111122223333:key/demo"


@pytest.mark.asyncio
async def test_loop_records_revoke_on_first_tick_within_configured_interval():
    event = asyncio.Event()
    recorded = []
    adapter = _FakeAdapter(
        provider="aws-kms",
        key_id="arn:aws:kms:us-east-1:111122223333:key/demo",
        metadata={"KeyMetadata": {"KeyState": "Disabled"}},
    )

    def _record(result):
        recorded.append(result)
        event.set()

    task = asyncio.create_task(
        detector.run_detection_loop(
            interval_s=0.05,
            load_checks=lambda: [detector.CMEKKeyCheck("t-acme", adapter)],
            record_result=_record,
        )
    )
    try:
        await asyncio.wait_for(event.wait(), timeout=1.0)
    finally:
        task.cancel()
        await task

    assert len(recorded) == 1
    assert recorded[0].revoked is True
    assert adapter.describe_calls == 1


def test_recorded_latest_results_are_process_local_observability_snapshot():
    result = detector.CMEKHealthResult(
        tenant_id="t-acme",
        provider="aws-kms",
        key_id="arn:aws:kms:us-east-1:111122223333:key/demo",
        ok=False,
        revoked=True,
        reason="describe_failed",
        checked_at=1.0,
        elapsed_ms=2.0,
        raw_state="KMSOperationError",
        detail={"error": "denied"},
    )

    detector.record_cmek_health_result(result)

    assert detector.latest_cmek_health_results() == [result.to_dict()]


def test_main_lifespan_starts_and_cancels_cmek_revoke_detector():
    source = pathlib.Path("backend/main.py").read_text()

    assert "cmek_revoke_detector" in source
    assert "cmek_revoke_task = asyncio.create_task" in source
    assert "cmek_revoke_task" in source.split("for t in", 1)[1]


def test_source_fingerprint_clean():
    source = pathlib.Path("backend/security/cmek_revoke_detector.py").read_text()
    fingerprint = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    assert not fingerprint.search(source)

"""KS.2.7 -- customer SIEM ingest contract tests."""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from backend.security import cmek_siem


AWS_KEY_ID = (
    "arn:aws:kms:us-east-1:111122223333:key/"
    "00000000-0000-0000-0000-000000000000"
)
GCP_KEY_ID = (
    "projects/acme-prod/locations/us/keyRings/omnisight/"
    "cryptoKeys/tenant-tier2"
)


def test_aws_omnisight_tags_are_cloudtrail_ingestable():
    tags = cmek_siem.build_omnisight_tags("aws-kms", tenant_id="t-acme")

    assert tags == {
        "OmniSight": "true",
        "OmniSightTenantId": "t-acme",
        "OmniSightControl": "cmek",
        "OmniSightSchema": "ks.2.7",
    }


def test_gcp_omnisight_labels_are_cloud_audit_logs_ingestable():
    tags = cmek_siem.build_omnisight_tags("gcp-kms", tenant_id="t-acme")

    assert tags == {
        "omnisight": "true",
        "omnisight_tenant_id": "t-acme",
        "omnisight_control": "cmek",
        "omnisight_schema": "ks-2-7",
    }


def test_cloudtrail_ingest_spec_filters_key_events_and_omnisight_context():
    spec = cmek_siem.build_cmek_siem_ingest_spec(
        "aws-kms",
        tenant_id="t-acme",
        key_id=AWS_KEY_ID,
        principal="arn:aws:iam::444455556666:role/OmniSightCMEKAccess",
    )

    body = spec.to_dict()
    assert body["source"] == "aws-cloudtrail"
    assert body["required_events"] == ["Encrypt", "Decrypt", "DescribeKey"]
    assert "kms.amazonaws.com" in body["ingest_filter"]
    assert AWS_KEY_ID in body["ingest_filter"]
    assert "arn:aws:iam::444455556666:role/OmniSightCMEKAccess" in body["ingest_filter"]
    assert body["sample_event"]["requestParameters"]["encryptionContext"] == {
        "tenant_id": "t-acme",
        "schema": "ks.1.2",
        "purpose": "cmek-tenant-dek",
    }
    assert body["sample_event"]["omnisight_tags"]["OmniSightTenantId"] == "t-acme"
    assert "S3 + SIEM forwarder" in body["customer_ingest_targets"]


def test_gcp_cloud_audit_logs_spec_filters_key_events_and_labels():
    spec = cmek_siem.build_cmek_siem_ingest_spec(
        "gcp-kms",
        tenant_id="t-acme",
        key_id=GCP_KEY_ID,
        principal="omnisight-cmek@example.iam.gserviceaccount.com",
    )

    body = spec.to_dict()
    assert body["source"] == "gcp-cloud-audit-logs"
    assert body["required_events"] == ["Encrypt", "Decrypt", "GetCryptoKey"]
    assert 'resource.type="cloudkms_cryptokey"' in body["ingest_filter"]
    assert GCP_KEY_ID in body["ingest_filter"]
    assert "omnisight-cmek@example.iam.gserviceaccount.com" in body["ingest_filter"]
    assert body["sample_event"]["resource"]["labels"] == {
        "omnisight": "true",
        "omnisight_tenant_id": "t-acme",
        "omnisight_control": "cmek",
        "omnisight_schema": "ks-2-7",
    }
    assert "Pub/Sub + SIEM forwarder" in body["customer_ingest_targets"]


def test_vault_transit_is_rejected_for_cloud_native_siem_specs():
    with pytest.raises(ValueError, match="aws-kms and gcp-kms"):
        cmek_siem.normalise_siem_provider("vault-transit")


def test_siem_spec_rejects_bad_tenant_or_key_shape():
    with pytest.raises(ValueError, match="tenant_id"):
        cmek_siem.build_cmek_siem_ingest_spec(
            "aws-kms",
            tenant_id="acme",
            key_id=AWS_KEY_ID,
        )

    with pytest.raises(ValueError, match="invalid gcp-kms key id"):
        cmek_siem.build_cmek_siem_ingest_spec(
            "gcp-kms",
            tenant_id="t-acme",
            key_id="projects/acme-prod/cryptoKeys/nope",
        )


@pytest.mark.asyncio
async def test_siem_ingest_endpoint_returns_stable_json(monkeypatch):
    from backend.routers import cmek_wizard

    async def allow_guard(_tenant_id, _actor):
        return None

    monkeypatch.setattr(cmek_wizard, "_guard", allow_guard)

    response = await cmek_wizard.generate_cmek_siem_ingest_spec(
        "t-acme",
        cmek_wizard.SIEMIngestSpecRequest(
            provider="aws-kms",
            key_id=AWS_KEY_ID,
            principal="arn:aws:iam::444455556666:role/OmniSightCMEKAccess",
        ),
        None,
        None,
    )
    body = json.loads(response.body)

    assert body["tenant_id"] == "t-acme"
    assert body["provider"] == "aws-kms"
    assert body["source"] == "aws-cloudtrail"
    assert body["omnisight_tags"]["OmniSightSchema"] == "ks.2.7"
    assert json.loads(body["ingest_spec_json"])["key_id"] == AWS_KEY_ID


def test_router_exposes_siem_ingest_endpoint():
    from backend.routers.cmek_wizard import router

    paths = {(tuple(sorted(r.methods)), r.path) for r in router.routes}

    assert (("POST",), "/tenants/{tenant_id}/cmek/siem/ingest-spec") in paths


def test_main_app_mounts_siem_ingest_endpoint():
    from backend.main import app

    paths = {
        (tuple(sorted(r.methods or [])), r.path)
        for r in app.routes
        if hasattr(r, "path")
    }

    assert (("POST",), "/api/v1/tenants/{tenant_id}/cmek/siem/ingest-spec") in paths


def test_runbook_documents_cloudtrail_cloud_audit_logs_and_customer_ingest():
    text = pathlib.Path("docs/ops/cmek_siem_ingest.md").read_text(encoding="utf-8")

    assert "CloudTrail" in text
    assert "Cloud Audit Logs" in text
    assert "OmniSightTenantId" in text
    assert "omnisight_tenant_id" in text
    assert "SIEM" in text
    assert "/cmek/siem/ingest-spec" in text


def test_source_fingerprint_clean():
    for path in [
        "backend/security/cmek_siem.py",
        "backend/routers/cmek_wizard.py",
    ]:
        source = pathlib.Path(path).read_text()
        fingerprint = re.compile(
            r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
        )
        assert not fingerprint.search(source)

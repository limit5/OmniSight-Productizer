"""KS.2.7 -- customer SIEM ingest specs for CMEK provider audit logs.

Tier 2 customers already own the KMS control plane, so their native
CloudTrail / Cloud Audit Logs streams are the source of truth for
Encrypt / Decrypt / DescribeKey activity. This module generates the
OmniSight tag or label set and provider-specific SIEM filter snippets
that a tenant admin can paste into their log pipeline.

Module-global state audit (SOP Step 1)
--------------------------------------
Only immutable constants live at module scope. Every worker derives the
same tag set and ingest filter from request input; no mutable cache,
singleton, network client, or filesystem state is introduced.

Read-after-write timing audit (SOP Step 1)
------------------------------------------
The helpers are pure request/response transforms and do not write PG,
Redis, cloud resources, or files. There is no read-after-write timing
contract to preserve.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from backend.security import cmek_wizard


CMEK_SIEM_PROVIDER = Literal["aws-kms", "gcp-kms"]
SIEM_SCHEMA = "ks.2.7"

_TENANT_ID_RE = re.compile(r"^t-[a-z0-9][a-z0-9-]{2,62}$")


@dataclass(frozen=True)
class CMEKSIEMIngestSpec:
    provider: CMEK_SIEM_PROVIDER
    tenant_id: str
    key_id: str
    source: str
    omnisight_tags: dict[str, str]
    ingest_filter: str
    customer_ingest_targets: tuple[str, ...]
    required_events: tuple[str, ...]
    sample_event: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "tenant_id": self.tenant_id,
            "key_id": self.key_id,
            "source": self.source,
            "omnisight_tags": dict(self.omnisight_tags),
            "ingest_filter": self.ingest_filter,
            "customer_ingest_targets": list(self.customer_ingest_targets),
            "required_events": list(self.required_events),
            "sample_event": dict(self.sample_event),
        }


def normalise_siem_provider(raw: str) -> CMEK_SIEM_PROVIDER:
    provider = cmek_wizard.normalise_provider(raw)
    if provider == "vault-transit":
        raise ValueError("customer SIEM ingest specs support aws-kms and gcp-kms")
    return provider


def validate_tenant_id(tenant_id: str) -> str:
    tenant_id = tenant_id.strip()
    if not _TENANT_ID_RE.match(tenant_id):
        raise ValueError("tenant_id must match t-<slug>")
    return tenant_id


def build_omnisight_tags(
    provider: CMEK_SIEM_PROVIDER,
    *,
    tenant_id: str,
) -> dict[str, str]:
    tenant_id = validate_tenant_id(tenant_id)
    if provider == "aws-kms":
        return {
            "OmniSight": "true",
            "OmniSightTenantId": tenant_id,
            "OmniSightControl": "cmek",
            "OmniSightSchema": SIEM_SCHEMA,
        }
    if provider == "gcp-kms":
        return {
            "omnisight": "true",
            "omnisight_tenant_id": tenant_id,
            "omnisight_control": "cmek",
            "omnisight_schema": "ks-2-7",
        }
    raise ValueError("provider must be one of aws-kms, gcp-kms")


def build_cmek_siem_ingest_spec(
    provider: CMEK_SIEM_PROVIDER,
    *,
    tenant_id: str,
    key_id: str,
    principal: str | None = None,
) -> CMEKSIEMIngestSpec:
    provider = normalise_siem_provider(provider)
    tenant_id = validate_tenant_id(tenant_id)
    key_id = cmek_wizard.validate_key_id(provider, key_id)
    tags = build_omnisight_tags(provider, tenant_id=tenant_id)

    if provider == "aws-kms":
        return _aws_cloudtrail_spec(
            tenant_id=tenant_id,
            key_id=key_id,
            principal=principal,
            tags=tags,
        )
    return _gcp_cloud_audit_logs_spec(
        tenant_id=tenant_id,
        key_id=key_id,
        principal=principal,
        tags=tags,
    )


def stable_ingest_json(spec: CMEKSIEMIngestSpec | dict[str, Any]) -> str:
    payload = spec.to_dict() if isinstance(spec, CMEKSIEMIngestSpec) else spec
    return json.dumps(payload, indent=2, sort_keys=True)


def _aws_cloudtrail_spec(
    *,
    tenant_id: str,
    key_id: str,
    principal: str | None,
    tags: dict[str, str],
) -> CMEKSIEMIngestSpec:
    account_id = key_id.split(":")[4]
    role_filter = (
        f" AND userIdentity.sessionContext.sessionIssuer.arn = '{principal}'"
        if principal
        else ""
    )
    ingest_filter = (
        "eventSource = 'kms.amazonaws.com' "
        "AND eventName IN ('Encrypt','Decrypt','DescribeKey') "
        f"AND recipientAccountId = '{account_id}' "
        f"AND resources.ARN = '{key_id}'"
        f"{role_filter}"
    )
    sample_event = {
        "eventSource": "kms.amazonaws.com",
        "eventName": "Encrypt",
        "requestParameters": {
            "encryptionContext": {
                "tenant_id": tenant_id,
                "schema": "ks.1.2",
                "purpose": "cmek-tenant-dek",
            }
        },
        "resources": [{"ARN": key_id, "type": "AWS::KMS::Key"}],
        "omnisight_tags": tags,
    }
    return CMEKSIEMIngestSpec(
        provider="aws-kms",
        tenant_id=tenant_id,
        key_id=key_id,
        source="aws-cloudtrail",
        omnisight_tags=tags,
        ingest_filter=ingest_filter,
        customer_ingest_targets=(
            "CloudTrail Lake",
            "S3 + SIEM forwarder",
            "EventBridge partner destination",
        ),
        required_events=("Encrypt", "Decrypt", "DescribeKey"),
        sample_event=sample_event,
    )


def _gcp_cloud_audit_logs_spec(
    *,
    tenant_id: str,
    key_id: str,
    principal: str | None,
    tags: dict[str, str],
) -> CMEKSIEMIngestSpec:
    principal_filter = (
        f'\nprotoPayload.authenticationInfo.principalEmail="{principal}"'
        if principal
        else ""
    )
    ingest_filter = (
        'resource.type="cloudkms_cryptokey"\n'
        'protoPayload.serviceName="cloudkms.googleapis.com"\n'
        f'protoPayload.resourceName="{key_id}"'
        f"{principal_filter}"
    )
    sample_event = {
        "resource": {
            "type": "cloudkms_cryptokey",
            "labels": tags,
        },
        "protoPayload": {
            "serviceName": "cloudkms.googleapis.com",
            "methodName": "Encrypt",
            "resourceName": key_id,
            "authenticationInfo": {
                "principalEmail": principal or "omnisight-cmek@example.iam.gserviceaccount.com",
            },
        },
        "omnisight_tags": tags,
        "omnisight_tenant_id": tenant_id,
    }
    return CMEKSIEMIngestSpec(
        provider="gcp-kms",
        tenant_id=tenant_id,
        key_id=key_id,
        source="gcp-cloud-audit-logs",
        omnisight_tags=tags,
        ingest_filter=ingest_filter,
        customer_ingest_targets=(
            "Log Router sink",
            "Pub/Sub + SIEM forwarder",
            "BigQuery linked dataset",
        ),
        required_events=("Encrypt", "Decrypt", "GetCryptoKey"),
        sample_event=sample_event,
    )


__all__ = [
    "CMEKSIEMIngestSpec",
    "SIEM_SCHEMA",
    "build_cmek_siem_ingest_spec",
    "build_omnisight_tags",
    "normalise_siem_provider",
    "stable_ingest_json",
    "validate_tenant_id",
]

"""KS.2.1 -- stateless CMEK tenant-settings wizard helpers.

This module owns only the onboarding wizard surface:

* provider catalogue for AWS / GCP / Vault;
* customer-side IAM / policy JSON generation;
* key-id shape validation; and
* a local test encrypt-decrypt probe used by the wizard before the
  durable KS.2.11 tables and KS.2.2-KS.2.4 live adapters land.

No customer credential, KMS token, or wizard draft is persisted here.
The tenant settings page keeps the step state in React state and the
``complete`` endpoint returns a non-durable Tier 2 draft summary.

Module-global state audit (SOP Step 1)
--------------------------------------
Only immutable provider metadata and regex constants are kept at module
scope. Every worker derives the same policy JSON from request input and
the verify probe delegates key material to ``LocalFernetKMSAdapter`` /
``secret_store`` disk coordination; no mutable process-local cache is
introduced.

Read-after-write timing audit (SOP Step 1)
------------------------------------------
The helpers are pure request/response transforms and do not write
shared state, so there is no downstream read-after-write timing
contract to preserve.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Literal

from backend.security.kms_adapters import LocalFernetKMSAdapter


CMEK_PROVIDER = Literal["aws-kms", "gcp-kms", "vault-transit"]

_AWS_KEY_RE = re.compile(
    r"^arn:aws:kms:[a-z0-9-]+:\d{12}:key/[0-9a-fA-F-]{36}$"
)
_AWS_PRINCIPAL_RE = re.compile(
    r"^arn:aws(?:-[a-z]+)?:iam::\d{12}:(?:role|user)/[A-Za-z0-9+=,.@_/-]{1,128}$"
)
_GCP_KEY_RE = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/[a-z0-9-]+/"
    r"keyRings/[A-Za-z0-9_-]{1,63}/cryptoKeys/[A-Za-z0-9_-]{1,63}$"
)
_GCP_MEMBER_RE = re.compile(r"^(?:serviceAccount|user|group|domain):[^\s@]+(?:@[^\s@]+\.[^\s@]+)?$")
_VAULT_KEY_RE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9_-]{0,63}/)?[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
)


@dataclass(frozen=True)
class CMEKProviderSpec:
    provider: CMEK_PROVIDER
    label: str
    key_id_label: str
    key_id_example: str
    policy_target_label: str
    policy_target_example: str

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "label": self.label,
            "key_id_label": self.key_id_label,
            "key_id_example": self.key_id_example,
            "policy_target_label": self.policy_target_label,
            "policy_target_example": self.policy_target_example,
        }


CMEK_PROVIDERS: tuple[CMEKProviderSpec, ...] = (
    CMEKProviderSpec(
        provider="aws-kms",
        label="AWS KMS",
        key_id_label="KMS key ARN",
        key_id_example="arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000",
        policy_target_label="OmniSight IAM role ARN",
        policy_target_example="arn:aws:iam::444455556666:role/OmniSightCMEKAccess",
    ),
    CMEKProviderSpec(
        provider="gcp-kms",
        label="Google Cloud KMS",
        key_id_label="CryptoKey resource id",
        key_id_example="projects/acme-prod/locations/us/keyRings/omnisight/cryptoKeys/tenant-tier2",
        policy_target_label="OmniSight service account",
        policy_target_example="serviceAccount:omnisight-cmek@omnisight-prod.iam.gserviceaccount.com",
    ),
    CMEKProviderSpec(
        provider="vault-transit",
        label="HashiCorp Vault Transit",
        key_id_label="Transit key name",
        key_id_example="transit/omnisight-tenant-tier2",
        policy_target_label="Vault entity or token display name",
        policy_target_example="omnisight-cmek",
    ),
)


def list_provider_specs() -> list[dict[str, str]]:
    return [spec.to_dict() for spec in CMEK_PROVIDERS]


def normalise_provider(raw: str) -> CMEK_PROVIDER:
    key = raw.strip().lower().replace("_", "-")
    if key in {"aws", "aws-kms"}:
        return "aws-kms"
    if key in {"gcp", "gcp-kms", "google-kms", "google-cloud-kms"}:
        return "gcp-kms"
    if key in {"vault", "vault-transit", "hashicorp-vault"}:
        return "vault-transit"
    raise ValueError("provider must be one of aws-kms, gcp-kms, vault-transit")


def validate_key_id(provider: CMEK_PROVIDER, key_id: str) -> str:
    key_id = key_id.strip()
    if provider == "aws-kms" and _AWS_KEY_RE.match(key_id):
        return key_id
    if provider == "gcp-kms" and _GCP_KEY_RE.match(key_id):
        return key_id
    if provider == "vault-transit" and _VAULT_KEY_RE.match(key_id):
        return key_id
    raise ValueError(f"invalid {provider} key id")


def validate_policy_principal(provider: CMEK_PROVIDER, principal: str) -> str:
    principal = principal.strip()
    if provider == "aws-kms" and _AWS_PRINCIPAL_RE.match(principal):
        return principal
    if provider == "gcp-kms" and _GCP_MEMBER_RE.match(principal):
        return principal
    if provider == "vault-transit" and _VAULT_KEY_RE.match(principal):
        return principal
    raise ValueError(f"invalid {provider} policy principal")


def _tenant_context_b64(provider: CMEK_PROVIDER, tenant_id: str) -> str:
    context = {
        "omnisight:cmek_provider": provider,
        "omnisight:tenant_id": tenant_id,
    }
    raw = json.dumps(context, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _vault_policy_paths(key_id: str | None) -> tuple[str, str]:
    ref = key_id.strip() if key_id else "transit/omnisight-tenant-tier2"
    if "/" in ref:
        mount_point, key_name = ref.split("/", 1)
    else:
        mount_point, key_name = "transit", ref
    return (
        f"{mount_point}/encrypt/{key_name}",
        f"{mount_point}/decrypt/{key_name}",
    )


def generate_policy_json(
    provider: CMEK_PROVIDER,
    *,
    tenant_id: str,
    principal: str,
    key_id: str | None = None,
) -> dict:
    principal = validate_policy_principal(provider, principal)
    if key_id:
        key_id = validate_key_id(provider, key_id)

    if provider == "aws-kms":
        return {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AllowOmniSightDescribeTenantKey",
                    "Effect": "Allow",
                    "Principal": {"AWS": principal},
                    "Action": "kms:DescribeKey",
                    "Resource": "*",
                },
                {
                    "Sid": "AllowOmniSightTenantEnvelopeEncryption",
                    "Effect": "Allow",
                    "Principal": {"AWS": principal},
                    "Action": ["kms:Encrypt", "kms:Decrypt"],
                    "Resource": "*",
                    "Condition": {
                        "StringEquals": {
                            "kms:EncryptionContext:omnisight:tenant_id": tenant_id,
                        },
                    },
                },
            ],
        }

    if provider == "gcp-kms":
        resource_name = key_id or "projects/PROJECT/locations/LOCATION/keyRings/RING/cryptoKeys/KEY"
        return {
            "bindings": [
                {
                    "role": "roles/cloudkms.cryptoKeyEncrypterDecrypter",
                    "members": [principal],
                    "condition": {
                        "title": "omnisight_tenant_cmek",
                        "description": f"Limit OmniSight CMEK use to tenant {tenant_id}",
                        "expression": f'resource.name == "{resource_name}"',
                    },
                }
            ]
        }

    encrypt_path, decrypt_path = _vault_policy_paths(key_id)
    context_b64 = _tenant_context_b64(provider, tenant_id)
    return {
        "policy": {
            "name": f"omnisight-{tenant_id}-cmek",
            "rules": [
                {
                    "path": encrypt_path,
                    "capabilities": ["update"],
                    "allowed_parameters": {
                        "context": [context_b64],
                    },
                },
                {
                    "path": decrypt_path,
                    "capabilities": ["update"],
                    "allowed_parameters": {
                        "context": [context_b64],
                    },
                },
            ],
            "attach_to": principal,
        },
    }


def verify_connection_probe(
    provider: CMEK_PROVIDER,
    *,
    tenant_id: str,
    key_id: str,
) -> dict[str, str | float | bool]:
    """Run the KS.2.1 wizard probe without touching live cloud accounts.

    KS.2.2-KS.2.4 own live SDK calls. This row verifies that the key id
    accepted by the wizard can complete the same wrap/unwrap contract
    using a local adapter and the final tenant encryption context shape.
    """

    key_id = validate_key_id(provider, key_id)
    started = time.perf_counter()
    plaintext = b"ks-2-1-cmek-wizard-probe:" + secrets.token_bytes(32)
    context = {
        "omnisight:tenant_id": tenant_id,
        "omnisight:cmek_provider": provider,
    }
    adapter = LocalFernetKMSAdapter(key_id="ks-2-1-wizard-probe")
    wrapped = adapter.wrap_dek(plaintext, encryption_context=context)
    decrypted = adapter.unwrap_dek(wrapped, encryption_context=context)
    if decrypted != plaintext:
        raise RuntimeError("CMEK wizard probe decrypt mismatch")
    return {
        "ok": True,
        "provider": provider,
        "key_id": key_id,
        "verification_id": f"cmekv_{secrets.token_hex(8)}",
        "algorithm": wrapped.algorithm,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "live_provider_checked": False,
    }


def stable_policy_json(policy: dict) -> str:
    return json.dumps(policy, indent=2, sort_keys=True)


__all__ = [
    "CMEK_PROVIDER",
    "CMEK_PROVIDERS",
    "generate_policy_json",
    "list_provider_specs",
    "normalise_provider",
    "stable_policy_json",
    "validate_key_id",
    "verify_connection_probe",
]

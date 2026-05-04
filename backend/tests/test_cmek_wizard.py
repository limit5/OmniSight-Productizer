"""KS.2.1 -- CMEK tenant settings wizard contract tests."""

from __future__ import annotations

import base64
import inspect
import json
import re

import pytest


def test_provider_catalog_exposes_aws_gcp_vault_only():
    from backend.security import cmek_wizard as cmek

    providers = cmek.list_provider_specs()

    assert [p["provider"] for p in providers] == [
        "aws-kms",
        "gcp-kms",
        "vault-transit",
    ]
    assert all("key_id_example" in p for p in providers)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("aws", "aws-kms"),
        ("AWS_KMS", "aws-kms"),
        ("gcp", "gcp-kms"),
        ("google-cloud-kms", "gcp-kms"),
        ("vault", "vault-transit"),
        ("hashicorp-vault", "vault-transit"),
    ],
)
def test_normalise_provider_aliases(raw, expected):
    from backend.security import cmek_wizard as cmek

    assert cmek.normalise_provider(raw) == expected


def test_normalise_provider_rejects_unknown():
    from backend.security import cmek_wizard as cmek

    with pytest.raises(ValueError, match="provider must be one of"):
        cmek.normalise_provider("local-fernet")


@pytest.mark.parametrize(
    ("provider", "key_id"),
    [
        (
            "aws-kms",
            "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000",
        ),
        (
            "gcp-kms",
            "projects/acme-prod/locations/us/keyRings/omnisight/cryptoKeys/tenant-tier2",
        ),
        ("vault-transit", "transit/omnisight-tenant-tier2"),
    ],
)
def test_validate_key_id_accepts_provider_shapes(provider, key_id):
    from backend.security import cmek_wizard as cmek

    assert cmek.validate_key_id(provider, key_id) == key_id


@pytest.mark.parametrize(
    ("provider", "key_id"),
    [
        ("aws-kms", "arn:aws:s3:::bucket"),
        ("gcp-kms", "projects/x/cryptoKeys/nope"),
        ("vault-transit", "../secret"),
    ],
)
def test_validate_key_id_rejects_bad_shapes(provider, key_id):
    from backend.security import cmek_wizard as cmek

    with pytest.raises(ValueError):
        cmek.validate_key_id(provider, key_id)


def test_aws_policy_is_precise_to_tenant_context():
    from backend.security import cmek_wizard as cmek

    policy = cmek.generate_policy_json(
        "aws-kms",
        tenant_id="t-acme",
        principal="arn:aws:iam::444455556666:role/OmniSightCMEKAccess",
        key_id="arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000",
    )

    describe_stmt, crypto_stmt = policy["Statement"]
    assert describe_stmt == {
        "Sid": "AllowOmniSightDescribeTenantKey",
        "Effect": "Allow",
        "Principal": {"AWS": "arn:aws:iam::444455556666:role/OmniSightCMEKAccess"},
        "Action": "kms:DescribeKey",
        "Resource": "*",
    }
    assert crypto_stmt["Effect"] == "Allow"
    assert crypto_stmt["Action"] == ["kms:Encrypt", "kms:Decrypt"]
    assert crypto_stmt["Resource"] == "*"
    assert crypto_stmt["Condition"]["StringEquals"] == {
        "kms:EncryptionContext:omnisight:tenant_id": "t-acme",
    }


def test_gcp_policy_uses_crypto_key_role():
    from backend.security import cmek_wizard as cmek

    policy = cmek.generate_policy_json(
        "gcp-kms",
        tenant_id="t-acme",
        principal="serviceAccount:omnisight-cmek@example.iam.gserviceaccount.com",
        key_id="projects/acme-prod/locations/us/keyRings/omnisight/cryptoKeys/tenant-tier2",
    )

    assert policy["bindings"][0]["role"] == "roles/cloudkms.cryptoKeyEncrypterDecrypter"
    assert policy["bindings"][0]["members"] == [
        "serviceAccount:omnisight-cmek@example.iam.gserviceaccount.com",
    ]
    assert policy["bindings"][0]["condition"]["expression"] == (
        'resource.name == "projects/acme-prod/locations/us/keyRings/omnisight/'
        'cryptoKeys/tenant-tier2"'
    )


def test_vault_policy_json_has_encrypt_and_decrypt_rules():
    from backend.security import cmek_wizard as cmek

    policy = cmek.generate_policy_json(
        "vault-transit",
        tenant_id="t-acme",
        principal="omnisight-cmek",
        key_id="transit/omnisight-tenant-tier2",
    )

    paths = [rule["path"] for rule in policy["policy"]["rules"]]
    assert paths == [
        "transit/encrypt/omnisight-tenant-tier2",
        "transit/decrypt/omnisight-tenant-tier2",
    ]
    assert all(rule["capabilities"] == ["update"] for rule in policy["policy"]["rules"])
    expected_context = base64.b64encode(
        json.dumps(
            {
                "omnisight:cmek_provider": "vault-transit",
                "omnisight:tenant_id": "t-acme",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    assert all(
        rule["allowed_parameters"] == {"context": [expected_context]}
        for rule in policy["policy"]["rules"]
    )


@pytest.mark.parametrize(
    ("provider", "principal"),
    [
        ("aws-kms", "arn:aws:iam::444455556666:root"),
        ("gcp-kms", "omnisight@example.iam.gserviceaccount.com"),
        ("vault-transit", "../omnisight"),
    ],
)
def test_policy_generation_rejects_non_pasteable_principals(provider, principal):
    from backend.security import cmek_wizard as cmek

    with pytest.raises(ValueError, match="policy principal"):
        cmek.generate_policy_json(
            provider,
            tenant_id="t-acme",
            principal=principal,
            key_id=None,
        )


def test_verify_connection_probe_runs_omnisight_encrypt_decrypt(monkeypatch):
    from backend import secret_store
    from backend.security import cmek_wizard as cmek

    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-2-1-cmek-wizard-test")
    secret_store._reset_for_tests()
    result = cmek.verify_connection_probe(
        "aws-kms",
        tenant_id="t-acme",
        key_id="arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000",
    )

    assert result["ok"] is True
    assert result["provider"] == "aws-kms"
    assert result["operation"] == "encrypt-decrypt"
    assert result["algorithm"] == "AES-256-GCM"
    assert result["wrap_algorithm"] == "fernet"
    assert result["live_provider_checked"] is False
    assert str(result["verification_id"]).startswith("cmekv_")
    assert "plaintext" not in result
    assert "ciphertext" not in result


def test_key_id_request_trims_and_validates_provider_shape():
    from backend.routers.cmek_wizard import KeyIdCMEKRequest
    from backend.security import cmek_wizard as cmek

    req = KeyIdCMEKRequest(
        provider="gcp-kms",
        key_id=" projects/acme-prod/locations/us/keyRings/omnisight/cryptoKeys/tenant-tier2 ",
    )

    assert cmek.validate_key_id(req.provider, req.key_id) == (
        "projects/acme-prod/locations/us/keyRings/omnisight/cryptoKeys/tenant-tier2"
    )


def test_router_exposes_five_step_endpoints():
    from backend.routers.cmek_wizard import router

    paths = {(tuple(sorted(r.methods)), r.path) for r in router.routes}

    assert (("GET",), "/tenants/{tenant_id}/cmek/wizard/providers") in paths
    assert (("POST",), "/tenants/{tenant_id}/cmek/wizard/policy") in paths
    assert (("POST",), "/tenants/{tenant_id}/cmek/wizard/key-id") in paths
    assert (("POST",), "/tenants/{tenant_id}/cmek/wizard/verify") in paths
    assert (("POST",), "/tenants/{tenant_id}/cmek/wizard/complete") in paths


def test_main_app_mounts_cmek_wizard_routes():
    from backend.main import app

    paths = {
        (tuple(sorted(r.methods or [])), r.path)
        for r in app.routes
        if hasattr(r, "path")
    }

    assert (("GET",), "/api/v1/tenants/{tenant_id}/cmek/wizard/providers") in paths
    assert (("POST",), "/api/v1/tenants/{tenant_id}/cmek/wizard/complete") in paths


def test_complete_request_requires_verify_token():
    from pydantic import ValidationError
    from backend.routers.cmek_wizard import CompleteCMEKRequest

    with pytest.raises(ValidationError):
        CompleteCMEKRequest(
            provider="aws-kms",
            key_id="arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000",
            verification_id="manual",
        )


def test_router_uses_current_user_dependency():
    from fastapi.params import Depends as _DependsParam
    from backend import auth
    from backend.routers import cmek_wizard

    deps = [
        p.default
        for p in inspect.signature(cmek_wizard.verify_cmek_wizard_connection)
        .parameters.values()
        if isinstance(p.default, _DependsParam)
    ]

    assert any(getattr(dep, "dependency", None) is auth.current_user for dep in deps)


def test_source_fingerprint_clean():
    import pathlib

    source = pathlib.Path("backend/routers/cmek_wizard.py").read_text()
    fingerprint = re.compile(
        r"_conn\(\)|await conn\.commit\(\)|datetime\('now'\)|VALUES.*\?[,)]"
    )
    assert not fingerprint.search(source)

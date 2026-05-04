"""KS.2.1 -- CMEK tenant settings wizard contract tests."""

from __future__ import annotations

import inspect
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

    stmt = policy["Statement"][0]
    assert stmt["Effect"] == "Allow"
    assert stmt["Action"] == ["kms:DescribeKey", "kms:Encrypt", "kms:Decrypt"]
    assert stmt["Condition"]["StringEquals"] == {
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


def test_vault_policy_json_has_encrypt_and_decrypt_rules():
    from backend.security import cmek_wizard as cmek

    policy = cmek.generate_policy_json(
        "vault-transit",
        tenant_id="t-acme",
        principal="omnisight-cmek",
        key_id="transit/omnisight-tenant-tier2",
    )

    paths = [rule["path"] for rule in policy["policy"]["rules"]]
    assert paths == ["transit/encrypt/{{key_name}}", "transit/decrypt/{{key_name}}"]
    assert all(rule["capabilities"] == ["update"] for rule in policy["policy"]["rules"])


def test_verify_connection_probe_round_trips_without_live_provider(monkeypatch):
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
    assert result["algorithm"] == "fernet"
    assert result["live_provider_checked"] is False
    assert str(result["verification_id"]).startswith("cmekv_")


def test_router_exposes_five_step_endpoints():
    from backend.routers.cmek_wizard import router

    paths = {(tuple(sorted(r.methods)), r.path) for r in router.routes}

    assert (("GET",), "/tenants/{tenant_id}/cmek/wizard/providers") in paths
    assert (("POST",), "/tenants/{tenant_id}/cmek/wizard/policy") in paths
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

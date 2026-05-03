"""KS.1.1 -- KMS adapter abstraction contract tests."""

from __future__ import annotations

import base64
import inspect
import sys
from types import ModuleType
from types import SimpleNamespace

import pytest

from backend import secret_store
from backend.security import kms_adapters as kms


class TestKMSProviderFactory:

    def test_list_providers_enumerates_four(self):
        assert kms.list_providers() == [
            "aws-kms",
            "gcp-kms",
            "vault-transit",
            "local-fernet",
        ]

    @pytest.mark.parametrize(
        "key,cls_name",
        [
            ("aws-kms", "AWSKMSAdapter"),
            ("aws", "AWSKMSAdapter"),
            ("gcp-kms", "GCPKMSAdapter"),
            ("google-cloud-kms", "GCPKMSAdapter"),
            ("vault-transit", "VaultTransitKMSAdapter"),
            ("vault", "VaultTransitKMSAdapter"),
            ("local-fernet", "LocalFernetKMSAdapter"),
            ("LOCAL_FERNET", "LocalFernetKMSAdapter"),
        ],
    )
    def test_get_adapter_resolves_known(self, key, cls_name):
        cls = kms.get_adapter(key)
        assert cls.__name__ == cls_name

    def test_get_adapter_rejects_unknown(self):
        with pytest.raises(ValueError) as excinfo:
            kms.get_adapter("azure")
        assert "Unknown KMS provider" in str(excinfo.value)
        for provider in kms.list_providers():
            assert provider in str(excinfo.value)

    def test_adapter_classes_satisfy_protocol_shape(self):
        for provider in kms.list_providers():
            cls = kms.get_adapter(provider)
            assert callable(getattr(cls, "wrap_dek"))
            assert callable(getattr(cls, "unwrap_dek"))


class TestWrappedDEK:

    def test_to_dict_base64_encodes_ciphertext_and_omits_raw(self):
        wrapped = kms.WrappedDEK(
            provider="aws-kms",
            key_id="arn:aws:kms:demo",
            ciphertext=b"\x00\x01cipher",
            key_version="v1",
            algorithm="SYMMETRIC_DEFAULT",
            encryption_context={"tenant_id": "t1"},
            raw={"secret": "provider-side-detail"},
        )

        data = wrapped.to_dict()

        assert data["ciphertext_b64"] == base64.b64encode(b"\x00\x01cipher").decode("ascii")
        assert "raw" not in data
        assert "provider-side-detail" not in repr(data)


class TestLocalFernetKMSAdapter:

    def test_round_trip_uses_existing_secret_store_key(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-1-local-fernet-test")
        secret_store._reset_for_tests()
        adapter = kms.LocalFernetKMSAdapter()
        plaintext = b"dek-" + (b"x" * 32)

        wrapped = adapter.wrap_dek(
            plaintext,
            encryption_context={"tenant_id": "tenant-a", "purpose": "oauth"},
        )

        assert wrapped.provider == "local-fernet"
        assert wrapped.key_id == "local-fernet"
        assert wrapped.ciphertext != plaintext
        assert adapter.unwrap_dek(
            wrapped,
            encryption_context={"tenant_id": "tenant-a", "purpose": "oauth"},
        ) == plaintext

    def test_context_mismatch_rejected(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks-1-1-local-fernet-context")
        secret_store._reset_for_tests()
        adapter = kms.LocalFernetKMSAdapter()
        wrapped = adapter.wrap_dek(b"dek-123", encryption_context={"tenant_id": "t1"})

        with pytest.raises(kms.KMSOperationError, match="encryption_context mismatch"):
            adapter.unwrap_dek(wrapped, encryption_context={"tenant_id": "t2"})

    def test_provider_mismatch_rejected(self):
        adapter = kms.LocalFernetKMSAdapter()
        wrapped = kms.WrappedDEK(
            provider="aws-kms",
            key_id="k",
            ciphertext=b"not-local",
        )

        with pytest.raises(kms.KMSOperationError, match="does not match"):
            adapter.unwrap_dek(wrapped)


class FakeAWSKMSClient:
    def __init__(self):
        self.encrypt_calls = []
        self.decrypt_calls = []

    def encrypt(self, **kwargs):
        self.encrypt_calls.append(kwargs)
        return {
            "KeyId": kwargs["KeyId"],
            "CiphertextBlob": b"aws-wrapped-" + kwargs["Plaintext"],
            "EncryptionAlgorithm": "SYMMETRIC_DEFAULT",
            "ResponseMetadata": {"RequestId": "req-1"},
        }

    def decrypt(self, **kwargs):
        self.decrypt_calls.append(kwargs)
        return {"Plaintext": kwargs["CiphertextBlob"].removeprefix(b"aws-wrapped-")}


class FakeAWSSTSClient:
    def __init__(self):
        self.assume_role_calls = []

    def assume_role(self, **kwargs):
        self.assume_role_calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "ASIAFAKE",
                "SecretAccessKey": "fake-secret",
                "SessionToken": "fake-session-token",
            }
        }


class FakeBoto3Module:
    def __init__(self):
        self.sts = FakeAWSSTSClient()
        self.kms = FakeAWSKMSClient()
        self.client_calls = []

    def client(self, service_name, **kwargs):
        self.client_calls.append((service_name, kwargs))
        if service_name == "sts":
            return self.sts
        if service_name == "kms":
            return self.kms
        raise AssertionError(f"unexpected boto3 service {service_name}")


class TestAWSKMSAdapter:

    def test_wrap_and_unwrap_delegates_to_boto3_client_shape(self):
        fake = FakeAWSKMSClient()
        adapter = kms.AWSKMSAdapter(
            key_id="arn:aws:kms:us-east-1:111122223333:key/demo",
            region_name="us-east-1",
        )
        adapter._client = fake

        wrapped = adapter.wrap_dek(b"dek-aws", encryption_context={"tenant_id": "t1"})
        plain = adapter.unwrap_dek(wrapped, encryption_context={"tenant_id": "t1"})

        assert plain == b"dek-aws"
        assert wrapped.provider == "aws-kms"
        assert wrapped.algorithm == "SYMMETRIC_DEFAULT"
        assert fake.encrypt_calls[0]["EncryptionContext"] == {"tenant_id": "t1"}
        assert fake.decrypt_calls[0]["KeyId"] == adapter.key_id

    def test_assume_role_builds_kms_client_with_temporary_credentials(self, monkeypatch):
        fake_boto3 = FakeBoto3Module()
        monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
        adapter = kms.AWSKMSAdapter(
            key_id="arn:aws:kms:us-east-1:111122223333:key/demo",
            region_name="us-east-1",
            role_arn="arn:aws:iam::111122223333:role/OmniSightKMS",
            external_id="tenant-a",
            session_name="omnisight-tenant-a",
        )

        wrapped = adapter.wrap_dek(b"dek-aws", encryption_context={"tenant_id": "t1"})

        assert wrapped.provider == "aws-kms"
        assert fake_boto3.sts.assume_role_calls == [
            {
                "RoleArn": "arn:aws:iam::111122223333:role/OmniSightKMS",
                "RoleSessionName": "omnisight-tenant-a",
                "ExternalId": "tenant-a",
            }
        ]
        assert fake_boto3.client_calls == [
            ("sts", {"region_name": "us-east-1"}),
            (
                "kms",
                {
                    "region_name": "us-east-1",
                    "aws_access_key_id": "ASIAFAKE",
                    "aws_secret_access_key": "fake-secret",
                    "aws_session_token": "fake-session-token",
                },
            ),
        ]
        assert fake_boto3.kms.encrypt_calls[0]["KeyId"] == adapter.key_id


class FakeGCPKMSClient:
    def __init__(self):
        self.encrypt_requests = []
        self.decrypt_requests = []

    def encrypt(self, *, request):
        self.encrypt_requests.append(request)
        return SimpleNamespace(
            ciphertext=b"gcp-wrapped-" + request["plaintext"],
            name=request["name"] + "/cryptoKeyVersions/1",
        )

    def decrypt(self, *, request):
        self.decrypt_requests.append(request)
        return SimpleNamespace(plaintext=request["ciphertext"].removeprefix(b"gcp-wrapped-"))


class FakeGoogleCloudKMSModule:
    def __init__(self):
        self.client = FakeGCPKMSClient()
        self.client_creations = 0

    def KeyManagementServiceClient(self):
        self.client_creations += 1
        return self.client


class TestGCPKMSAdapter:

    def test_wrap_and_unwrap_delegates_to_google_client_shape(self):
        fake = FakeGCPKMSClient()
        adapter = kms.GCPKMSAdapter(
            key_id="projects/p/locations/global/keyRings/r/cryptoKeys/k"
        )
        adapter._client = fake

        wrapped = adapter.wrap_dek(b"dek-gcp", encryption_context={"tenant_id": "t1"})
        plain = adapter.unwrap_dek(wrapped, encryption_context={"tenant_id": "t1"})

        assert plain == b"dek-gcp"
        assert wrapped.provider == "gcp-kms"
        assert wrapped.key_version.endswith("/cryptoKeyVersions/1")
        assert fake.encrypt_requests[0]["name"] == adapter.key_id
        assert fake.encrypt_requests[0]["additional_authenticated_data"] == (
            b'{"tenant_id":"t1"}'
        )
        assert fake.decrypt_requests[0]["additional_authenticated_data"] == (
            b'{"tenant_id":"t1"}'
        )

    def test_lazy_google_cloud_kms_client_is_used_when_not_injected(self, monkeypatch):
        fake_kms = FakeGoogleCloudKMSModule()
        google_module = ModuleType("google")
        cloud_module = ModuleType("google.cloud")
        cloud_module.kms = fake_kms
        monkeypatch.setitem(sys.modules, "google", google_module)
        monkeypatch.setitem(sys.modules, "google.cloud", cloud_module)
        monkeypatch.setitem(sys.modules, "google.cloud.kms", fake_kms)
        adapter = kms.GCPKMSAdapter(
            key_id="projects/p/locations/global/keyRings/r/cryptoKeys/k"
        )

        wrapped = adapter.wrap_dek(b"dek-gcp", encryption_context={"tenant_id": "t1"})

        assert wrapped.provider == "gcp-kms"
        assert fake_kms.client_creations == 1
        assert fake_kms.client.encrypt_requests[0]["name"] == adapter.key_id
        assert fake_kms.client.encrypt_requests[0]["plaintext"] == b"dek-gcp"
        assert fake_kms.client.encrypt_requests[0]["additional_authenticated_data"] == (
            b'{"tenant_id":"t1"}'
        )


class FakeVaultTransit:
    def __init__(self):
        self.encrypt_calls = []
        self.decrypt_calls = []

    def encrypt_data(self, **kwargs):
        self.encrypt_calls.append(kwargs)
        return {"data": {"ciphertext": "vault:v7:" + kwargs["plaintext"]}}

    def decrypt_data(self, **kwargs):
        self.decrypt_calls.append(kwargs)
        return {"data": {"plaintext": kwargs["ciphertext"].split(":", 2)[2]}}


class TestVaultTransitKMSAdapter:

    def test_wrap_and_unwrap_delegates_to_hvac_transit_shape(self):
        transit = FakeVaultTransit()
        adapter = kms.VaultTransitKMSAdapter(
            key_id="tenant-dek",
            url="https://vault.example.com",
            token="vault-token",
            mount_point="transit-prod",
        )
        adapter._client = SimpleNamespace(secrets=SimpleNamespace(transit=transit))

        wrapped = adapter.wrap_dek(b"dek-vault", encryption_context={"tenant_id": "t1"})
        plain = adapter.unwrap_dek(wrapped, encryption_context={"tenant_id": "t1"})

        assert plain == b"dek-vault"
        assert wrapped.provider == "vault-transit"
        assert wrapped.key_version == "v7"
        assert transit.encrypt_calls[0]["name"] == "tenant-dek"
        assert transit.encrypt_calls[0]["mount_point"] == "transit-prod"
        assert transit.decrypt_calls[0]["ciphertext"].startswith("vault:v7:")


class TestConfigAndDriftGuards:

    def test_requires_non_empty_dek_bytes(self):
        adapter = kms.LocalFernetKMSAdapter()
        with pytest.raises(kms.KMSConfigurationError, match="plaintext_dek"):
            adapter.wrap_dek(b"")

    def test_cloud_sdk_imports_are_lazy(self):
        source = inspect.getsource(kms)
        assert "import boto3" in source
        assert "from google.cloud import kms" in source
        assert "import hvac" in source
        assert "boto3.client" in source
        assert "KeyManagementServiceClient" in source
        assert "hvac.Client" in source

    def test_no_module_global_mutable_registry(self):
        module_globals = vars(kms)
        assert "_client" not in module_globals
        assert "_adapter_registry" not in module_globals

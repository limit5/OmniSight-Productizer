"""FS.3.1 -- Tests for the shared storage provisioning adapter base + factory."""

from __future__ import annotations

import pytest

from backend import secret_store
from backend.storage_provisioning import (
    StorageProvisionAdapter,
    StorageProvisionError,
    StorageProvisionResult,
    get_adapter,
    list_providers,
)
from backend.storage_provisioning.base import StorageProvisionRateLimitError


class TestStorageProvisionProviderFactory:

    def test_list_providers_enumerates_three(self):
        assert list_providers() == ["s3", "r2", "supabase-storage"]

    @pytest.mark.parametrize(
        "key,cls_name",
        [
            ("s3", "S3StorageProvisionAdapter"),
            ("r2", "R2StorageProvisionAdapter"),
            ("supabase-storage", "SupabaseStorageProvisionAdapter"),
            ("supabase", "SupabaseStorageProvisionAdapter"),
            ("SUPABASE_STORAGE", "SupabaseStorageProvisionAdapter"),
        ],
    )
    def test_get_adapter_resolves_known(self, key, cls_name):
        cls = get_adapter(key)
        assert cls.__name__ == cls_name
        assert issubclass(cls, StorageProvisionAdapter)

    def test_get_adapter_rejects_unknown(self):
        with pytest.raises(ValueError) as excinfo:
            get_adapter("gcs")
        assert "Unknown storage provisioning provider" in str(excinfo.value)
        for provider in list_providers():
            assert provider in str(excinfo.value)

    def test_every_adapter_has_unique_provider_classvar(self):
        seen = set()
        for provider in list_providers():
            cls = get_adapter(provider)
            assert cls.provider
            assert cls.provider not in seen
            seen.add(cls.provider)


class TestEncryptedTokenFactory:

    def test_from_encrypted_token_decrypts_via_secret_store(self, monkeypatch):
        monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-fixture-key-fs-3-1")
        secret_store._reset_for_tests()

        plaintext = "aws_secret_abcdef0123456789"
        ciphertext = secret_store.encrypt(plaintext)

        adapter_cls = get_adapter("s3")
        adapter = adapter_cls.from_encrypted_token(
            ciphertext,
            bucket_name="tenant-demo",
            access_key_id="AKIA0123456789",
        )
        assert isinstance(adapter, StorageProvisionAdapter)
        assert adapter.bucket_name == "tenant-demo"
        fp = adapter.token_fp()
        assert fp.endswith("6789")
        assert plaintext not in fp

    def test_from_plaintext_token_bypasses_secret_store(self):
        adapter_cls = get_adapter("r2")
        adapter = adapter_cls.from_plaintext_token(
            "r2_secret_1234567890",
            bucket_name="assets",
            access_key_id="r2_access",
            account_id="acct_123",
        )
        assert adapter.bucket_name == "assets"


class TestStorageProvisionResult:

    def test_to_dict(self):
        result = StorageProvisionResult(
            provider="s3",
            bucket_name="tenant-demo",
            bucket_id="tenant-demo",
            endpoint_url="https://s3.amazonaws.com",
            public_url=None,
            status="ready",
            created=True,
            region="us-east-1",
        )
        data = result.to_dict()
        assert data["provider"] == "s3"
        assert data["bucket_name"] == "tenant-demo"
        assert data["bucket_id"] == "tenant-demo"
        assert data["created"] is True
        assert data["region"] == "us-east-1"


class TestInterfaceContract:

    @pytest.mark.parametrize("provider", ["s3", "r2", "supabase-storage"])
    def test_required_methods_present(self, provider):
        cls = get_adapter(provider)
        assert callable(getattr(cls, "provision_bucket"))
        assert callable(getattr(cls, "get_bucket_config"))

    def test_cannot_instantiate_base_directly(self):
        with pytest.raises(TypeError):
            StorageProvisionAdapter(token="t", bucket_name="p")  # type: ignore[abstract]

    def test_rate_limit_error_is_storage_provision_error_subclass(self):
        assert issubclass(StorageProvisionRateLimitError, StorageProvisionError)

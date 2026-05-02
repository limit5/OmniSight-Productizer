"""FS.1.1 — Tests for the shared DB provisioning adapter base + factory."""

from __future__ import annotations

import pytest

from backend import secret_store
from backend.db_provisioning import (
    DBProvisionAdapter,
    DBProvisionError,
    DatabaseProvisionResult,
    get_adapter,
    list_providers,
)
from backend.db_provisioning.base import DBProvisionRateLimitError


class TestDBProvisionProviderFactory:

    def test_list_providers_enumerates_three(self):
        assert list_providers() == ["supabase", "neon", "planetscale"]

    @pytest.mark.parametrize(
        "key,cls_name",
        [
            ("supabase", "SupabaseDBProvisionAdapter"),
            ("neon", "NeonDBProvisionAdapter"),
            ("planetscale", "PlanetScaleDBProvisionAdapter"),
            ("planet-scale", "PlanetScaleDBProvisionAdapter"),
            ("PLANETSCALE", "PlanetScaleDBProvisionAdapter"),
        ],
    )
    def test_get_adapter_resolves_known(self, key, cls_name):
        cls = get_adapter(key)
        assert cls.__name__ == cls_name
        assert issubclass(cls, DBProvisionAdapter)

    def test_get_adapter_rejects_unknown(self):
        with pytest.raises(ValueError) as excinfo:
            get_adapter("rds")
        assert "Unknown DB provisioning provider" in str(excinfo.value)
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
        monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-fixture-key-fs-1-1")
        secret_store._reset_for_tests()

        plaintext = "sbp_abcdef0123456789"
        ciphertext = secret_store.encrypt(plaintext)

        adapter_cls = get_adapter("supabase")
        adapter = adapter_cls.from_encrypted_token(
            ciphertext,
            database_name="tenant-demo",
            organization_id="org_123",
        )
        assert isinstance(adapter, DBProvisionAdapter)
        assert adapter.database_name == "tenant-demo"
        fp = adapter.token_fp()
        assert fp.endswith("6789")
        assert plaintext not in fp

    def test_from_plaintext_token_bypasses_secret_store(self):
        adapter_cls = get_adapter("neon")
        adapter = adapter_cls.from_plaintext_token("napi_1234567890", database_name="db")
        assert adapter.database_name == "db"


class TestDatabaseProvisionResult:

    def test_to_dict(self):
        result = DatabaseProvisionResult(
            provider="neon",
            database_id="prj_1",
            database_name="tenant-demo",
            connection_url="postgresql://user:pass@example/db",
            status="ready",
            created=True,
            region="aws-us-east-1",
        )
        data = result.to_dict()
        assert data["provider"] == "neon"
        assert data["database_id"] == "prj_1"
        assert data["connection_url"].startswith("postgresql://")
        assert data["created"] is True


class TestInterfaceContract:

    @pytest.mark.parametrize("provider", ["supabase", "neon", "planetscale"])
    def test_required_methods_present(self, provider):
        cls = get_adapter(provider)
        for name in ("provision_database", "get_connection_url"):
            assert callable(getattr(cls, name)), f"{cls.__name__} missing {name}"

    def test_cannot_instantiate_base_directly(self):
        with pytest.raises(TypeError):
            DBProvisionAdapter(token="t", database_name="p")  # type: ignore[abstract]

    def test_rate_limit_error_is_db_provision_error_subclass(self):
        assert issubclass(DBProvisionRateLimitError, DBProvisionError)

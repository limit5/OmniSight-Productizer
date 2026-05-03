"""FS.2.1 -- Tests for the shared auth provisioning adapter base + factory."""

from __future__ import annotations

import pytest

from backend import secret_store
from backend.auth_provisioning import (
    DEFAULT_OIDC_SCOPES,
    AuthProviderSetupResult,
    AuthProvisionAdapter,
    AuthProvisionError,
    get_adapter,
    list_providers,
)
from backend.auth_provisioning.base import AuthProvisionRateLimitError


class TestAuthProvisionProviderFactory:

    def test_list_providers_enumerates_three(self):
        assert list_providers() == ["clerk", "auth0", "workos"]

    @pytest.mark.parametrize(
        "key,cls_name",
        [
            ("clerk", "ClerkAuthProvisionAdapter"),
            ("auth0", "Auth0AuthProvisionAdapter"),
            ("workos", "WorkOSAuthProvisionAdapter"),
            ("WORKOS", "WorkOSAuthProvisionAdapter"),
        ],
    )
    def test_get_adapter_resolves_known(self, key, cls_name):
        cls = get_adapter(key)
        assert cls.__name__ == cls_name
        assert issubclass(cls, AuthProvisionAdapter)

    def test_get_adapter_rejects_unknown(self):
        with pytest.raises(ValueError) as excinfo:
            get_adapter("firebase")
        assert "Unknown auth provisioning provider" in str(excinfo.value)
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
        monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-fixture-key-fs-2-1")
        secret_store._reset_for_tests()

        plaintext = "sk_test_abcdef0123456789"
        ciphertext = secret_store.encrypt(plaintext)

        adapter_cls = get_adapter("clerk")
        adapter = adapter_cls.from_encrypted_token(
            ciphertext,
            application_name="tenant-demo",
            created_by="user_123",
        )
        assert isinstance(adapter, AuthProvisionAdapter)
        assert adapter.application_name == "tenant-demo"
        fp = adapter.token_fp()
        assert fp.endswith("6789")
        assert plaintext not in fp

    def test_from_plaintext_token_bypasses_secret_store(self):
        adapter_cls = get_adapter("auth0")
        adapter = adapter_cls.from_plaintext_token(
            "mgmt_1234567890",
            application_name="app",
            tenant_domain="example.us.auth0.com",
        )
        assert adapter.application_name == "app"


class TestAuthProviderSetupResult:

    def test_to_dict(self):
        result = AuthProviderSetupResult(
            provider="auth0",
            application_id="client_123",
            application_name="tenant-demo",
            client_id="client_123",
            client_secret="secret",
            issuer_url="https://example.us.auth0.com/",
            redirect_uris=("https://app.example.com/api/auth/callback/auth0",),
            allowed_origins=("https://app.example.com",),
            created=True,
        )
        data = result.to_dict()
        assert data["provider"] == "auth0"
        assert data["application_id"] == "client_123"
        assert data["client_secret"] == "secret"
        assert data["redirect_uris"] == ["https://app.example.com/api/auth/callback/auth0"]
        assert data["allowed_origins"] == ["https://app.example.com"]
        assert data["scopes"] == list(DEFAULT_OIDC_SCOPES)


class TestInterfaceContract:

    @pytest.mark.parametrize("provider", ["clerk", "auth0", "workos"])
    def test_required_methods_present(self, provider):
        cls = get_adapter(provider)
        assert callable(getattr(cls, "setup_application"))
        assert callable(getattr(cls, "get_client_config"))

    def test_cannot_instantiate_base_directly(self):
        with pytest.raises(TypeError):
            AuthProvisionAdapter(token="t", application_name="p")  # type: ignore[abstract]

    def test_rate_limit_error_is_auth_provision_error_subclass(self):
        assert issubclass(AuthProvisionRateLimitError, AuthProvisionError)

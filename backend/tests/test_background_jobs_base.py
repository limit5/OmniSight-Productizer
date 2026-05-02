"""FS.5.1 -- Tests for the shared background job adapter base + factory."""

from __future__ import annotations

import pytest

from backend import secret_store
from backend.background_jobs import (
    BackgroundJobAdapter,
    BackgroundJobError,
    BackgroundJobRateLimitError,
    BackgroundJobRequest,
    BackgroundJobResult,
    get_adapter,
    list_providers,
)


class TestBackgroundJobProviderFactory:

    def test_list_providers_enumerates_three(self):
        assert list_providers() == ["inngest", "trigger-dev", "vercel-cron"]

    @pytest.mark.parametrize(
        "key,cls_name",
        [
            ("inngest", "InngestBackgroundJobAdapter"),
            ("trigger-dev", "TriggerDevBackgroundJobAdapter"),
            ("trigger", "TriggerDevBackgroundJobAdapter"),
            ("TRIGGER_DEV", "TriggerDevBackgroundJobAdapter"),
            ("vercel-cron", "VercelCronBackgroundJobAdapter"),
            ("vercel", "VercelCronBackgroundJobAdapter"),
        ],
    )
    def test_get_adapter_resolves_known(self, key, cls_name):
        cls = get_adapter(key)
        assert cls.__name__ == cls_name
        assert issubclass(cls, BackgroundJobAdapter)

    def test_get_adapter_rejects_unknown(self):
        with pytest.raises(ValueError) as excinfo:
            get_adapter("temporal")
        assert "Unknown background job provider" in str(excinfo.value)
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
        monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-fixture-key-fs-5-1")
        secret_store._reset_for_tests()

        plaintext = "inngest_abcdef0123456789"
        ciphertext = secret_store.encrypt(plaintext)

        adapter_cls = get_adapter("inngest")
        adapter = adapter_cls.from_encrypted_token(ciphertext, event_key="evt-key")
        assert isinstance(adapter, BackgroundJobAdapter)
        fp = adapter.token_fp()
        assert fp.endswith("6789")
        assert plaintext not in fp

    def test_from_plaintext_token_bypasses_secret_store(self):
        adapter = get_adapter("trigger").from_plaintext_token("tr_1234567890")
        assert adapter.provider == "trigger-dev"


class TestBackgroundJobRequest:

    def test_to_dict_normalizes_endpoint_path(self):
        req = BackgroundJobRequest(
            name="sync.catalog",
            payload={"tenant_id": "t1"},
            idempotency_key=" idem ",
            cron="*/5 * * * *",
            endpoint_path="api/cron/sync-catalog",
        )

        assert req.to_dict() == {
            "name": "sync.catalog",
            "payload": {"tenant_id": "t1"},
            "idempotency_key": "idem",
            "cron": "*/5 * * * *",
            "endpoint_path": "/api/cron/sync-catalog",
        }

    def test_requires_name(self):
        with pytest.raises(ValueError, match="job name"):
            BackgroundJobRequest(name="  ")


class TestBackgroundJobResult:

    def test_to_dict_omits_raw_payload(self):
        result = BackgroundJobResult(
            provider="inngest",
            job_id="evt_123",
            status="queued",
            raw={"token": "provider-secret"},
        )

        data = result.to_dict()

        assert data == {
            "provider": "inngest",
            "job_id": "evt_123",
            "status": "queued",
        }
        assert "provider-secret" not in repr(data)


class TestInterfaceContract:

    @pytest.mark.parametrize("provider", ["inngest", "trigger-dev", "vercel-cron"])
    def test_required_methods_present(self, provider):
        cls = get_adapter(provider)
        assert callable(getattr(cls, "dispatch_job"))
        assert callable(getattr(cls, "cron_descriptor"))

    def test_cannot_instantiate_base_directly(self):
        with pytest.raises(TypeError):
            BackgroundJobAdapter(token="t")  # type: ignore[abstract]

    def test_rate_limit_error_is_background_job_error_subclass(self):
        assert issubclass(BackgroundJobRateLimitError, BackgroundJobError)

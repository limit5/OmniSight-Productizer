"""FS.4.1 -- Tests for the shared email delivery adapter base + factory."""

from __future__ import annotations

import pytest

from backend import secret_store
from backend.email_delivery import (
    EmailAddress,
    EmailAttachment,
    EmailDeliveryAdapter,
    EmailDeliveryError,
    EmailDeliveryResult,
    EmailMessage,
    get_adapter,
    list_providers,
)
from backend.email_delivery.base import EmailDeliveryRateLimitError


def _message() -> EmailMessage:
    return EmailMessage(
        sender=EmailAddress("ops@example.com", "Ops"),
        to=[EmailAddress("alice@example.com")],
        subject="Welcome",
        text="hello",
    )


class TestEmailDeliveryProviderFactory:

    def test_list_providers_enumerates_three(self):
        assert list_providers() == ["resend", "postmark", "aws-ses"]

    @pytest.mark.parametrize(
        "key,cls_name",
        [
            ("resend", "ResendEmailDeliveryAdapter"),
            ("postmark", "PostmarkEmailDeliveryAdapter"),
            ("aws-ses", "SESEmailDeliveryAdapter"),
            ("ses", "SESEmailDeliveryAdapter"),
            ("AWS_SES", "SESEmailDeliveryAdapter"),
        ],
    )
    def test_get_adapter_resolves_known(self, key, cls_name):
        cls = get_adapter(key)
        assert cls.__name__ == cls_name
        assert issubclass(cls, EmailDeliveryAdapter)

    def test_get_adapter_rejects_unknown(self):
        with pytest.raises(ValueError) as excinfo:
            get_adapter("mailgun")
        assert "Unknown email delivery provider" in str(excinfo.value)
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
        monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "test-fixture-key-fs-4-1")
        secret_store._reset_for_tests()

        plaintext = "re_abcdef0123456789"
        ciphertext = secret_store.encrypt(plaintext)

        adapter_cls = get_adapter("resend")
        adapter = adapter_cls.from_encrypted_token(ciphertext)
        assert isinstance(adapter, EmailDeliveryAdapter)
        fp = adapter.token_fp()
        assert fp.endswith("6789")
        assert plaintext not in fp

    def test_from_plaintext_token_bypasses_secret_store(self):
        adapter = get_adapter("postmark").from_plaintext_token("pm_1234567890")
        assert adapter.provider == "postmark"


class TestEmailMessage:

    def test_address_formats_name(self):
        assert EmailAddress("ops@example.com", "Ops").formatted() == "Ops <ops@example.com>"

    def test_message_to_dict_omits_raw_payload(self):
        msg = EmailMessage(
            sender=EmailAddress("ops@example.com"),
            to=[EmailAddress("alice@example.com")],
            subject="Subject",
            html="<p>Hello</p>",
            tags={"template": "welcome"},
            attachments=[
                EmailAttachment(
                    filename="a.txt",
                    content="SGVsbG8=",
                    content_type="text/plain",
                ),
            ],
        )

        data = msg.to_dict()

        assert data["from"] == {"email": "ops@example.com"}
        assert data["to"] == [{"email": "alice@example.com"}]
        assert data["html"] == "<p>Hello</p>"
        assert data["tags"] == {"template": "welcome"}
        assert data["attachments"][0]["filename"] == "a.txt"

    def test_requires_recipient(self):
        with pytest.raises(ValueError, match="recipient"):
            EmailMessage(
                sender=EmailAddress("ops@example.com"),
                to=[],
                subject="Subject",
                text="hello",
            )

    def test_requires_body(self):
        with pytest.raises(ValueError, match="text or html"):
            EmailMessage(
                sender=EmailAddress("ops@example.com"),
                to=[EmailAddress("alice@example.com")],
                subject="Subject",
            )


class TestEmailDeliveryResult:

    def test_to_dict(self):
        result = EmailDeliveryResult(
            provider="resend",
            message_id="em_123",
            status="sent",
            accepted=["alice@example.com"],
            rejected=[],
            raw={"token": "provider-secret"},
        )

        data = result.to_dict()

        assert data == {
            "provider": "resend",
            "message_id": "em_123",
            "status": "sent",
            "accepted": ["alice@example.com"],
            "rejected": [],
        }
        assert "provider-secret" not in repr(data)


class TestInterfaceContract:

    @pytest.mark.parametrize("provider", ["resend", "postmark", "aws-ses"])
    def test_required_methods_present(self, provider):
        cls = get_adapter(provider)
        assert callable(getattr(cls, "send_email"))

    def test_cannot_instantiate_base_directly(self):
        with pytest.raises(TypeError):
            EmailDeliveryAdapter(token="t")  # type: ignore[abstract]

    def test_rate_limit_error_is_email_delivery_error_subclass(self):
        assert issubclass(EmailDeliveryRateLimitError, EmailDeliveryError)

    def test_message_fixture_is_valid(self):
        assert _message().subject == "Welcome"

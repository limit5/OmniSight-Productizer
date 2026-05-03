"""FS.4.2 -- Tests for the transactional email template registry."""

from __future__ import annotations

import pytest

from backend.email_delivery import (
    EMAIL_TEMPLATE_IDS,
    EMAIL_TEMPLATE_ITEMS,
    EMAIL_TEMPLATES,
    EmailAddress,
    EmailMessage,
    EmailTemplateRenderOptions,
    MissingEmailTemplateVariableError,
    get_email_template,
    list_email_templates,
    render_email_template,
)


def _options(template_id: str, **context):
    base = {
        "user_name": "Alice",
        "product_name": "OmniSight",
        "app_url": "https://app.example.com",
        "support_email": "support@example.com",
        "verification_url": "https://app.example.com/verify",
        "reset_url": "https://app.example.com/reset",
        "download_url": "https://app.example.com/export.zip",
        "request_id": "dsar_123",
        "request_type": "access",
        "due_at": "2026-06-02 00:00:00 UTC",
        "inviter_name": "Ops",
        "workspace_name": "Acme",
        "invite_url": "https://app.example.com/invite",
        "code": "123456",
        "expires_in": "15 minutes",
    }
    base.update(context)
    return EmailTemplateRenderOptions(
        template_id=template_id,
        sender=EmailAddress("ops@example.com", "Ops"),
        to=(EmailAddress("alice@example.com"),),
        context=base,
    )


class TestEmailTemplateRegistry:

    def test_list_email_templates_pins_fs_4_2_catalog(self):
        assert list_email_templates() == [
            "welcome",
            "email-verification",
            "password-reset",
            "dsar-request-received",
            "dsar-export-ready",
            "invite",
            "mfa-code",
        ]
        assert EMAIL_TEMPLATE_IDS == tuple(list_email_templates())

    def test_mapping_matches_catalog_items(self):
        assert tuple(item.template_id for item in EMAIL_TEMPLATE_ITEMS) == EMAIL_TEMPLATE_IDS
        assert tuple(EMAIL_TEMPLATES) == EMAIL_TEMPLATE_IDS

    @pytest.mark.parametrize("template_id", EMAIL_TEMPLATE_IDS)
    def test_get_email_template_returns_frozen_catalog_entry(self, template_id):
        item = get_email_template(template_id)
        assert item.template_id == template_id
        assert item.display_name
        assert item.subject
        assert item.text
        assert item.html
        assert item.required_variables

    def test_get_email_template_accepts_underscore_alias(self):
        assert get_email_template("password_reset").template_id == "password-reset"
        assert get_email_template("DSAR_REQUEST_RECEIVED").template_id == "dsar-request-received"
        assert get_email_template("DSAR_EXPORT_READY").template_id == "dsar-export-ready"

    def test_get_email_template_rejects_unknown(self):
        with pytest.raises(KeyError, match="unknown email template"):
            get_email_template("receipt")


class TestEmailTemplateRender:

    def test_render_welcome_template_returns_email_message(self):
        message = render_email_template(_options("welcome"))

        assert isinstance(message, EmailMessage)
        assert message.subject == "Welcome to OmniSight"
        assert message.sender.formatted() == "Ops <ops@example.com>"
        assert [a.email for a in message.to] == ["alice@example.com"]
        assert "https://app.example.com" in message.text
        assert '<a href="https://app.example.com">' in message.html
        assert message.tags == {"template": "welcome"}

    def test_render_preserves_headers_and_extra_tags(self):
        options = EmailTemplateRenderOptions(
            template_id="email-verification",
            sender=EmailAddress("ops@example.com"),
            to=(EmailAddress("alice@example.com"),),
            context=_options("email-verification").context,
            headers={"X-Trace": "trace-1"},
            tags={"tenant": "t-1"},
        )

        message = render_email_template(options)

        assert message.headers == {"X-Trace": "trace-1"}
        assert message.tags == {"template": "email-verification", "tenant": "t-1"}

    def test_render_escapes_context_values(self):
        message = render_email_template(
            _options(
                "password-reset",
                user_name="<script>alert(1)</script>",
                reset_url="https://app.example.com/reset?token=a&next=/",
            )
        )

        assert "<script>" not in message.html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in message.html
        assert "token=a&amp;next=/" in message.html
        assert "token=a&amp;next=/" in message.text

    def test_render_rejects_missing_required_variable(self):
        context = dict(_options("dsar-request-received").context)
        context.pop("due_at")

        with pytest.raises(
            MissingEmailTemplateVariableError,
            match="due_at",
        ):
            render_email_template(
                EmailTemplateRenderOptions(
                    template_id="dsar-request-received",
                    sender=EmailAddress("ops@example.com"),
                    to=(EmailAddress("alice@example.com"),),
                    context=context,
                )
            )

    def test_render_rejects_empty_template_id(self):
        with pytest.raises(ValueError, match="template_id"):
            render_email_template(_options(""))

    @pytest.mark.parametrize("template_id", EMAIL_TEMPLATE_IDS)
    def test_every_template_renders_valid_message(self, template_id):
        message = render_email_template(_options(template_id))

        assert message.subject
        assert message.text
        assert message.html
        assert message.tags["template"] == template_id

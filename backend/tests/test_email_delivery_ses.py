"""FS.4.1 -- AWS SES email delivery adapter tests (respx-mocked)."""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.email_delivery.base import (
    EmailAddress,
    EmailAttachment,
    EmailDeliveryError,
    MissingEmailDeliveryScopeError,
    EmailMessage,
)
from backend.email_delivery.ses import SESEmailDeliveryAdapter

S = "https://email.us-west-2.amazonaws.com"


def _mk_adapter(**kw):
    return SESEmailDeliveryAdapter(
        token="aws_secret_ABCDEF0123456789",
        access_key_id="AKIA0123456789",
        region="us-west-2",
        **kw,
    )


def _message() -> EmailMessage:
    return EmailMessage(
        sender=EmailAddress("ops@example.com", "Ops"),
        to=[EmailAddress("alice@example.com")],
        subject="Welcome",
        text="hello",
        html="<p>hello</p>",
        reply_to=[EmailAddress("support@example.com")],
        tags={"template": "welcome"},
    )


class TestSESEmailDelivery:

    @respx.mock
    async def test_send_email_happy(self):
        route = respx.post(f"{S}/v2/email/outbound-emails").mock(
            return_value=httpx.Response(200, json={"MessageId": "ses-123"}),
        )

        result = await _mk_adapter(configuration_set_name="prod").send_email(_message())

        assert result.provider == "aws-ses"
        assert result.message_id == "ses-123"
        assert result.accepted == ["alice@example.com"]
        req = route.calls.last.request
        assert req.headers["authorization"].startswith("AWS4-HMAC-SHA256")
        assert "Credential=AKIA0123456789/" in req.headers["authorization"]
        assert "/us-west-2/ses/aws4_request" in req.headers["authorization"]
        body = httpx.Response(200, content=req.read()).json()
        assert body["FromEmailAddress"] == "Ops <ops@example.com>"
        assert body["Destination"]["ToAddresses"] == ["alice@example.com"]
        assert body["ReplyToAddresses"] == ["support@example.com"]
        assert body["ConfigurationSetName"] == "prod"
        assert body["EmailTags"] == [{"Name": "template", "Value": "welcome"}]
        assert body["Content"]["Simple"]["Body"]["Text"]["Data"] == "hello"
        assert body["Content"]["Simple"]["Body"]["Html"]["Data"] == "<p>hello</p>"

    @respx.mock
    async def test_403_maps_to_missing_scope(self):
        respx.post(f"{S}/v2/email/outbound-emails").mock(
            return_value=httpx.Response(403, json={"message": "denied"}),
        )
        with pytest.raises(MissingEmailDeliveryScopeError):
            await _mk_adapter().send_email(_message())

    async def test_rejects_attachments(self):
        msg = EmailMessage(
            sender=EmailAddress("ops@example.com"),
            to=[EmailAddress("alice@example.com")],
            subject="Subject",
            text="hello",
            attachments=[EmailAttachment("a.txt", "SGVsbG8=")],
        )
        with pytest.raises(EmailDeliveryError, match="simple text/html"):
            await _mk_adapter().send_email(msg)

    @respx.mock
    async def test_missing_message_id_rejected(self):
        respx.post(f"{S}/v2/email/outbound-emails").mock(
            return_value=httpx.Response(200, json={}),
        )
        with pytest.raises(EmailDeliveryError, match="MessageId"):
            await _mk_adapter().send_email(_message())
